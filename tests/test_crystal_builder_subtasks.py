from common.config_processing import get_config
from crystal_modeller import Modeller
from crystal_building.utils import (rotvec2rotmat, build_unit_cell, scale_asymmetric_unit,
                                    align_crystaldata_to_principal_axes, batch_asymmetric_unit_pose_analysis_torch)
from scipy.spatial.transform import Rotation
from common.geometry_calculations import sph2rotvec, rotvec2sph, batch_molecule_principal_axes_torch
import numpy as np
import torch

'''
run tests on subtasks of the supercell builder
'''
'''load test dataset'''
config_path = r'C:/Users/mikem/OneDrive/NYU/CSD/MCryGAN/configs/test_configs/crystal_building.yaml'
user_path = r'C:/Users/mikem/OneDrive/NYU/CSD/MCryGAN/configs/users/mkilgour.yaml'
config = get_config(user_yaml_path=user_path, main_yaml_path=config_path)
modeller = Modeller(config)
_, data_loader, _ = modeller.load_dataset_and_dataloaders(override_test_fraction=1)
modeller.misc_pre_training_items()
supercell_builder = modeller.supercell_builder
test_crystals = next(iter(data_loader))

supercell_size = 5
rotation_basis = 'spherical'


class TestClass:
    def test_rotvec2rotmat(self):
        """
        confirm transformation from rotvec to rotation matrix in cartesian and spherical bases
        """
        '''check cartesian mode'''
        rotations = [Rotation.random() for _ in range(5)]
        rvecs = torch.stack([torch.Tensor(rotation.as_rotvec()) for rotation in rotations])

        rvecs2 = sph2rotvec(rotvec2sph(rvecs))
        assert (rvecs - rvecs2).abs().mean() < 1e-4

        rotmats = rotvec2rotmat(rvecs, basis='cartesian')
        check_rotmats = torch.stack([torch.Tensor(rotation.as_matrix()) for rotation in rotations])
        assert (rotmats - check_rotmats).abs().mean() < 1e-4

        '''check spherical mode'''
        rotations = [Rotation.random() for _ in range(5)]
        rvecs = rotvec2sph(torch.stack([torch.Tensor(rotation.as_rotvec()) for rotation in rotations]))
        rotmats = rotvec2rotmat(rvecs, basis='spherical')
        check_rotmats = torch.stack([torch.Tensor(rotation.as_matrix()) for rotation in rotations])
        assert (rotmats - check_rotmats).abs().mean() < 1e-4

        return None

    # todo doesn't currently work - have to set the pos argument as the canonical conformer which is not necessarily true
    def WIP_build_unit_cell(self):
        test_unit_cells = \
            build_unit_cell(test_crystals.mult.clone(),
                            [test_crystals.pos[test_crystals.batch == ii] for ii in range(test_crystals.num_graphs)],
                            test_crystals.T_fc.clone(),
                            torch.linalg.inv(test_crystals.T_fc),
                            [torch.Tensor(test_crystals.symmetry_operators[ii]) for ii in range(test_crystals.num_graphs)]
                            )

        disagreements = torch.stack([(test_unit_cells[ii] - test_crystals.ref_cell_pos[ii]).abs().sum() for ii in range(test_crystals.num_graphs)])
        assert disagreements.mean() < 1e-4
        return None

    # todo define an assertion - right now the function itself is the best check unless we do it manually for each SG
    def WIP_scale_asymmetric_unit(self):
        space_groups = torch.tensor(np.asarray(list(supercell_builder.asym_unit_dict.keys())).astype(int))
        centroid_coords = torch.Tensor(np.random.uniform(0, 1, size=(len(space_groups), 3)))
        scaled_centroids = scale_asymmetric_unit(supercell_builder.asym_unit_dict, mol_position=centroid_coords, sg_inds=space_groups)
        return None

    # todo this check may fail for high symmetry molecules - need either to get rid of them or find a way to deal with them
    def test_align_crystaldata_to_principal_axes(self):
        '''
        align some crystaldata to cartesian axes in natural handedness
        then check that this is what happened
        '''

        aligned_test_crystals = align_crystaldata_to_principal_axes(test_crystals.clone(),
                                                                    handedness=test_crystals.asym_unit_handedness)
        aligned_principal_axes, _, _ = \
            batch_molecule_principal_axes_torch(
                [aligned_test_crystals.pos[test_crystals.batch == ii] for ii in range(test_crystals.num_graphs)])

        alignment_check = torch.eye(3).tile(aligned_test_crystals.num_graphs, 1, 1)
        alignment_check[:, 0, 0] = test_crystals.asym_unit_handedness

        assert torch.mean(torch.abs(alignment_check - aligned_principal_axes)) < 1e-4
        return None

    # todo this is redundant, as this same function is used to define these parameters in dataset construction
    def WIP_batch_asymmetric_unit_pose_analysis(self):
        positions, orientations, handedness, well_defined_asym_unit, canonical_conformer_coords = (
            batch_asymmetric_unit_pose_analysis_torch(
                unit_cell_coords_list=[torch.Tensor(test_crystals.ref_cell_pos[ii]) for ii in range(test_crystals.num_graphs)],
                sg_ind_list=test_crystals.sg_ind,
                asym_unit_dict=supercell_builder.asym_unit_dict,
                T_fc_list=test_crystals.T_fc,
                enforce_right_handedness=False,
                rotation_basis='cartesian',
                return_asym_unit_coords=True
            ))

        '''confirm cell params agree with dataset construction'''
        assert (positions - test_crystals.cell_params[:, 6:9]).abs().mean() < 1e-4
        assert (orientations - test_crystals.cell_params[:, 9:12]).abs().mean() < 1e-4
        assert (handedness - test_crystals.asym_unit_handedness).abs().mean() < 1e-4

        return None
