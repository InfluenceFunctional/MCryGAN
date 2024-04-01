"""
standalone code for molecular crystal analyzer
requirements numpy, scipy, torch, torch_geometric, torch_scatter, torch_cluster, pyyaml, tqdm
"""
from pathlib import Path

import yaml

import torch
from torch_geometric.loader.dataloader import Collater
import numpy as np
from argparse import Namespace

from bulk_molecule_classification.utils import reload_model
from mxtaltools.common.config_processing import dict2namespace
from mxtaltools.common.ase_interface import ase_mol_from_crystaldata
from mxtaltools.common.geometry_calculations import cell_vol_torch
from mxtaltools.constants.atom_properties import VDW_RADII, ATOM_WEIGHTS, ELECTRONEGATIVITY, GROUP, PERIOD
from mxtaltools.crystal_building.builder import SupercellBuilder
from mxtaltools.dataset_management.CrystalData import CrystalData
from mxtaltools.models.discriminator_models import CrystalDiscriminator
from mxtaltools.models.regression_models import MoleculeRegressor
from mxtaltools.models.utils import softmax_and_score
from mxtaltools.models.vdw_overlap import vdw_overlap

import pathlib

module_path = str(pathlib.Path(__file__).parent.resolve())

config_path = module_path + '/crystal_analyzer.yaml'
discriminator_checkpoint_path = module_path + '/discriminator_checkpoint'
volume_checkpoint_path = module_path + '/regressor_checkpoint'


def load_yaml(path):
    yaml_path = Path(path)
    assert yaml_path.exists()
    assert yaml_path.suffix in {".yaml", ".yml"}
    with yaml_path.open("r") as f:
        target_dict = yaml.safe_load(f)

    return target_dict


class CrystalAnalyzer(torch.nn.Module):
    def __init__(self,
                 device, supercell_size=5):
        super(CrystalAnalyzer, self).__init__()

        self.device = device
        self.config = dict2namespace(load_yaml(config_path))
        self.supercell_size = supercell_size
        self.mol_asym_unit_vol = 63.33  # approximate urea asym unit volume

        # update configs from checkpoints
        checkpoint = torch.load(discriminator_checkpoint_path, map_location=self.device)
        model_config = Namespace(**checkpoint['config'])  # overwrite the settings for the model
        self.config.discriminator.optimizer = model_config.optimizer
        self.config.discriminator.model = model_config.model
        self.d_dataDims = checkpoint['dataDims']

        checkpoint = torch.load(volume_checkpoint_path, map_location=self.device)
        model_config = Namespace(**checkpoint['config'])  # overwrite the settings for the model
        self.config.regressor.optimizer = model_config.optimizer
        self.config.regressor.model = model_config.model
        self.r_dataDims = checkpoint['dataDims']

        num_atom_features = 6
        num_molecule_features = 2

        self.model = CrystalDiscriminator(seed=12345, config=self.config.discriminator.model,
                                          num_atom_features=num_atom_features,
                                          num_molecule_features=num_molecule_features)
        for param in self.model.parameters():  # freeze encoder
            param.requires_grad = False
        self.model, _ = reload_model(self.model, device=self.device, optimizer=None, path=discriminator_checkpoint_path)
        self.model.eval()
        self.model.to(self.device)

        self.volume_model = MoleculeRegressor(seed=12345, config=self.config.regressor.model,
                                              num_atom_features=num_atom_features,
                                              num_molecule_features=num_molecule_features)
        for param in self.volume_model.parameters():  # freeze encoder
            param.requires_grad = False
        self.volume_model, _ = reload_model(self.volume_model, device=self.device, optimizer=None,
                                            path=volume_checkpoint_path)
        self.volume_model.eval()
        self.volume_model.to(self.device)

        self.auvol_mean = self.r_dataDims['target_mean']
        self.auvol_std = self.r_dataDims['target_std']

        self.supercell_builder = SupercellBuilder(device=self.device, rotation_basis='spherical')
        self.vdw_radii = torch.tensor(np.nan_to_num(list(VDW_RADII.values())).astype('float'), dtype=torch.float32,
                                      device=self.device)
        self.atomic_masses = torch.tensor(np.nan_to_num(list(ATOM_WEIGHTS.values())).astype('float'),
                                          dtype=torch.float32,
                                          device=self.device)
        self.electronegativities = torch.tensor(np.nan_to_num(list(ELECTRONEGATIVITY.values())).astype('float'),
                                                dtype=torch.float32, device=self.device)
        self.atom_groups = torch.tensor(np.nan_to_num(list(GROUP.values())).astype('float'), dtype=torch.float32,
                                        device=self.device)
        self.atom_periods = torch.tensor(np.nan_to_num(list(PERIOD.values())).astype('float'), dtype=torch.float32,
                                         device=self.device)

        self.collater = Collater(0, 0)

        self.atom_standardization_vector = torch.zeros((num_atom_features, 2), device=self.device)
        self.mol_standardization_vector = torch.zeros((num_molecule_features, 2), device=self.device)

        self.atom_standardization_vector = torch.tensor(
            [self.d_dataDims['standardization_dict'][feat] for feat in self.d_dataDims['atom_features']],
            dtype=torch.float32, device=self.device)
        # don't adjust atomic numbers
        self.atom_standardization_vector[0, 0] = 0
        self.atom_standardization_vector[0, 1] = 1

        self.mol_standardization_vector = torch.tensor(
            [self.d_dataDims['standardization_dict'][feat] for feat in self.d_dataDims['molecule_features']],
            dtype=torch.float32, device=self.device)

    def __call__(self, coords_list: list, atom_types_list: list, mol_masses_list: list,
                 proposed_cell_params: torch.tensor,
                 proposed_sgs: torch.tensor,
                 score_type='heuristic',
                 return_stats=False,
                 n_top_k: int = 1):
        with torch.no_grad():
            data = self.prep_crystaldata(atom_types_list, coords_list, mol_masses_list)

            if score_type in ['classifier', 'rdf_distance',
                              'heuristic']:  # convert from intensive fractional cell parameter to full box params
                proposed_crystaldata = self.build_crystal(data, proposed_cell_params, proposed_sgs.long().tolist())

                discriminator_output, pair_dist_dict = self.adversarial_score(proposed_crystaldata)
                classification_score = softmax_and_score(discriminator_output[:, :2])
                predicted_distance = discriminator_output[:, -1]

                vdw_loss, vdw_score, _, _, _ = vdw_overlap(self.vdw_radii,
                                                           crystaldata=proposed_crystaldata,
                                                           return_score_only=False,
                                                           loss_func='inv')

                sample_auv = self.compute_aunit_volume(proposed_cell_params, proposed_crystaldata.mult)
                target_auv = self.estimate_aunit_volume(data)[:, 0]
                # packing_loss = (F.smooth_l1_loss(target_auv, sample_auv, reduction='none')/target_auv)
                # something finnicky with packing loss prediction right now - substitute for maximal density
                mol_num_atoms = data.ptr.diff()  # normalize volume by number of atoms - a crude normalization
                packing_loss = sample_auv / mol_num_atoms / 10
                heuristic_score = - vdw_loss - packing_loss

                if score_type == 'classifier':
                    output = classification_score
                elif score_type == 'rdf_distance':
                    output = -predicted_distance
                elif score_type == 'heuristic':
                    output = heuristic_score

                sort_inds = torch.argsort(predicted_distance)[-n_top_k:].cpu().detach().numpy()  # save top 10 samples
                mols = [ase_mol_from_crystaldata(proposed_crystaldata,
                                                 index=ind,
                                                 exclusion_level='distance',
                                                 inclusion_distance=6) for ind in sort_inds]

                if return_stats:
                    stats_dict = {
                        'log_vdw_loss': np.log10(vdw_loss.cpu().detach().numpy()),
                        'log_packing_loss': -np.log10(packing_loss.cpu().detach().numpy()),
                        'vdw_score': vdw_score.cpu().detach().numpy(),
                        'vdw_loss': vdw_loss.cpu().detach().numpy(),
                        'packing_loss': packing_loss.cpu().detach().numpy(),
                        'classification_score': classification_score.cpu().detach().numpy(),
                        'predicted_distance': (10 ** (predicted_distance) - 1).cpu().detach().numpy(),
                        'log_predicted_distance': np.log10((10 ** (predicted_distance) - 1).cpu().detach().numpy()),
                        'heuristic_score': heuristic_score.cpu().detach().numpy(),
                        'log_heuristic_loss': np.log10(-heuristic_score.cpu().detach().numpy()),
                        'sample_auv': sample_auv.cpu().detach().numpy(),
                        'target_auv': target_auv.cpu().detach().numpy(),
                        'topk_samples': mols,
                    }

                    return output, stats_dict
                else:
                    return output
            else:
                assert False, f"{score_type} is not an implemented crystal scoring function"

    def prep_crystaldata(self, atom_types_list, coords_list, mol_masses_list):
        # quick featurization
        atom_feats_list = []
        for ind, atoms in enumerate(atom_types_list):
            atom_feats = torch.zeros(len(atoms), 6)
            atom_feats[:, 0] = atoms
            atom_feats[:, 1] = self.atomic_masses[atoms]
            atom_feats[:, 2] = self.vdw_radii[atoms]
            atom_feats[:, 3] = self.electronegativities[atoms]
            atom_feats[:, 4] = self.atom_groups[atoms]
            atom_feats[:, 5] = self.atom_periods[atoms]
            atom_feats_list.append(atom_feats)

        datapoints = [
            CrystalData(
                x=atom_feats_list[ind],
                mol_x=torch.tensor([mol_masses_list[ind],
                                    len(atom_feats_list[ind])],
                                   dtype=torch.float32, device=self.device)[None, :],
                pos=coords_list[ind],
                y=torch.ones(1),
                tracking=torch.ones(1),
                mult=torch.ones(1),
                T_fc=torch.eye(3),
                mol_size=torch.ones(1) * len(atom_feats_list[ind]),
            )
            for ind in range(len(coords_list))
        ]
        data = self.collater(datapoints)
        data.to(self.device)
        # standardize input
        data.x = (data.x - self.atom_standardization_vector[:, 0]) / self.atom_standardization_vector[:, 1]
        data.mol_x = (data.mol_x - self.mol_standardization_vector[:, 0]) / self.mol_standardization_vector[:, 1]

        return data

    def compute_aunit_volume(self, cell_params, multiplicity):
        volumes_list = []
        for i in range(len(cell_params)):
            volumes_list.append(cell_vol_torch(cell_params[i, 0:3], cell_params[i, 3:6]))

        volumes_list = torch.tensor(volumes_list, dtype=torch.float32, device=self.device)
        return volumes_list / multiplicity

    def estimate_aunit_volume(self, data):
        model_aunit_volume = self.volume_model(data) * self.auvol_std + self.auvol_mean
        return model_aunit_volume

    def build_crystal(self, data, proposed_cell_params, proposed_sgs):
        data = self.prep_molecule_data(data, proposed_cell_params, proposed_sgs)
        # todo add parameter safety assertions
        proposed_crystaldata, proposed_cell_volumes = self.supercell_builder.build_supercells(
            data, proposed_cell_params, self.supercell_size,
            self.config.discriminator.model.convolution_cutoff,
            align_to_standardized_orientation=False,
            target_handedness=data.asym_unit_handedness,
            skip_refeaturization=True,
        )
        return proposed_crystaldata

    def prep_molecule_data(self, data, proposed_cell_params, proposed_sgs):
        data.symmetry_operators = [self.supercell_builder.symmetries_dict['sym_ops'][ind] for ind in proposed_sgs]
        data.sg_ind = proposed_sgs
        data.cell_params = proposed_cell_params
        data.mult = torch.tensor([
            len(sym_op) for sym_op in data.symmetry_operators
        ], device=data.x.device, dtype=torch.long)
        return data

    def adversarial_score(self, data):
        """
        get the score from the discriminator on data
        """
        output, extra_outputs = self.model(data.clone(), return_dists=True, return_latent=False)
        return output, extra_outputs['dists_dict']
