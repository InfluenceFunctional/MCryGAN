"""
standalone code for molecular crystal analyzer
requirements numpy, scipy, torch, torch_geometric, torch_scatter, torch_cluster, pyyaml, tqdm
"""
from pathlib import Path

import yaml

import torch
import torch.nn.functional as F

from bulk_molecule_classification.utils import reload_model
from common.config_processing import dict2namespace
from common.geometry_calculations import cell_vol_torch
from constants.atom_properties import VDW_RADII
from crystal_building.builder import SupercellBuilder
from models.discriminator_models import CrystalDiscriminator
from models.regression_models import MoleculeRegressor
from models.utils import softmax_and_score
from models.vdw_overlap import vdw_overlap

config_path = '/standalone/crystal_analyzer.yaml'
discriminator_checkpoint_path = '/cluster/best_discriminator_experiments_benchmarks_multi_discriminator_15-02-12-25-55'
volume_checkpoint_path = '/cluster/best_regressor_experiments_benchmarks_vol_regressor_15-02-12-25-32'


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

        self.model = CrystalDiscriminator(seed=12345, config=self.config.discriminator.model, num_atom_features=4, num_molecule_features=2)
        for param in self.model.parameters():  # freeze encoder
            param.requires_grad = False
        self.model = reload_model(self.model, device=self.device, optimizer=None, path=discriminator_checkpoint_path)
        self.model.eval()

        self.volume_model = MoleculeRegressor(seed=12345, config=self.config.regressor.model, num_atom_features=4, num_molecule_features=2)
        for param in self.volume_model.parameters():  # freeze encoder
            param.requires_grad = False
        self.volume_model = reload_model(self.volume_model, device=self.device, optimizer=None, path=volume_checkpoint_path)
        self.volume_model.eval()

        self.packing_mean = 628.2591876500782
        self.packing_std = 432.8356636345374

        self.supercell_builder = SupercellBuilder(device=self.device, rotation_basis='spherical')
        self.vdw_radii = VDW_RADII

    def forward(self, data, proposed_cell_params: torch.tensor, proposed_sgs: torch.tensor, score_type='heuristic', return_stats=False):
        with torch.no_grad():
            if score_type in ['classifier', 'rdf_distance', 'heuristic']:
                proposed_crystaldata = self.build_crystal(data, proposed_cell_params, proposed_sgs)

                discriminator_output, pair_dist_dict = self.adversarial_score(proposed_crystaldata)
                classification_score = softmax_and_score(discriminator_output[:, :2])
                predicted_distance = discriminator_output[:, -1]

                sample_predicted_aunit_volume = self.compute_aunit_volume(proposed_cell_params, proposed_crystaldata.mult)
                vdw_score = vdw_overlap(self.vdw_radii, crystaldata=proposed_crystaldata, return_score_only=True).cpu().detach().numpy()
                model_predicted_aunit_volme = self.estimate_aunit_volume(data)
                packing_loss = F.smooth_l1_loss(sample_predicted_aunit_volume, model_predicted_aunit_volme, reduction='none')

                heuristic_score = vdw_score * torch.exp(-packing_loss * 5)

                if score_type == 'classifier':
                    output = classification_score
                elif score_type == 'rdf_distance':
                    output = torch.exp(-predicted_distance) - 1
                elif score_type == 'heuristic':
                    output = heuristic_score

                if return_stats:
                    stats_dict = {
                        'classification_score': classification_score.cpu().detach().numpy(),
                        'predicted_distance': predicted_distance.cpu().detach().numpy(),
                        'heuristic_score': heuristic_score.cpu().detach().numpy(),
                        'predicted_packing_coeff': sample_predicted_aunit_volume.cpu().detach().numpy(),
                        'model_packing_coeff': model_predicted_aunit_volme.cpu().detach().numpy(),
                        'vdw_score': vdw_score.cpu().detach().numpy(),
                    }
                    return output, stats_dict
                else:
                    return output

            elif score_type == 'density':
                data = self.preprocess(data, proposed_sgs)
                sample_predicted_aunit_volume = self.compute_aunit_volume(data.cell_params, data.mult)
                model_predicted_aunit_volme = self.estimate_aunit_volume(data)
                packing_loss = F.smooth_l1_loss(sample_predicted_aunit_volume, model_predicted_aunit_volme, reduction='none')
                output = torch.exp(-5 * packing_loss)

                if return_stats:
                    stats_dict = {
                        'predicted_packing_coeff': sample_predicted_aunit_volume.cpu().detach().numpy(),
                        'model_packing_coeff': model_predicted_aunit_volme.cpu().detach().numpy(),
                    }
                    return output, stats_dict
                else:
                    return output

    @staticmethod
    def compute_aunit_volume(cell_params, multiplicity):
        volumes_list = []
        for i in range(len(cell_params)):
            volumes_list.append(cell_vol_torch(cell_params[i, 0:3], cell_params[i, 3:6]))

        return cell_params / multiplicity

    def estimate_aunit_volume(self, data):
        model_packing_coeff = self.volume_model(data) * self.packing_std + self.packing_mean
        return model_packing_coeff

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
