import torch
import numpy as np
from common.utils import standardize
from dataset_management.CrystalData import CrystalData
from crystal_building.utils import batch_asymmetric_unit_pose_analysis_torch
from constants.asymmetric_units import asym_unit_dict as asymmetric_unit_dict
import sys
from torch_geometric.loader import DataLoader
import tqdm
import pandas as pd
from pyxtal import symmetry
from dataset_management.manager import DataManager
import os
from torch_geometric.loader.dataloader import Collater
from new_dataset_management.featurization_utils import get_range_fraction


class TrainingDataBuilder:
    """
    build dataset object
    """

    def __init__(self, config, dataset_path=None, data_std_path=None, preloaded_dataset=None, data_std_dict=None, override_length=None):
        self.crystal_generation_features = None
        self.crystal_keys = None
        self.tracking_keys = None
        self.lattice_keys = None

        self.regression_target = config.regression_target
        self.dataset_seed = config.seed
        self.single_molecule_dataset_identifier = config.single_molecule_dataset_identifier
        np.random.seed(self.dataset_seed)

        self.model_mode = config.mode

        if override_length is not None:
            self.max_dataset_length = override_length
        else:
            self.max_dataset_length = config.max_dataset_length

        '''
        load the dataset
        '''
        if preloaded_dataset is not None:
            dataset = preloaded_dataset
            self.std_dict = data_std_dict
        elif dataset_path is not None:
            dataset = pd.read_pickle(dataset_path)
            misc_data_dict = np.load(data_std_path, allow_pickle=True).item()
            self.std_dict = misc_data_dict['standardization_dict']
        else:
            assert False, "Must feed a path to a dataset or a dataset itself"

        self.dataset_length = min(len(dataset), self.max_dataset_length)

        # shuffle and cut up dataset before processing
        dataset = dataset.loc[np.random.choice(len(dataset), self.dataset_length, replace=False)]
        dataset = self.last_minute_featurization_and_one_hots(dataset, config)  # add a few odds & ends

        '''identify keys to load & track'''
        self.atom_keys = config.atom_feature_keys
        self.molecule_keys = config.molecule_feature_keys
        self.set_crystal_keys()
        self.set_tracking_keys()
        self.set_crystal_generation_keys(dataset)

        '''
        prep for modelling
        '''
        self.datapoints = self.generate_training_datapoints(dataset)  # todo consider namespace args

        if config.single_molecule_dataset_identifier is not None:  # make dataset a bunch of the same molecule
            identifiers = [item.csd_identifier for item in self.datapoints]
            index = identifiers.index(self.single_molecule_dataset_identifier)  # PIQTOY # VEJCES reasonably flat molecule # NICOAM03 from the paper fig
            new_datapoints = [self.datapoints[index] for i in range(self.dataset_length)]
            self.datapoints = new_datapoints

        self.dataDims = self.get_dimension()

        # '''flag for test dataset construction'''
        # self.build_dataset_for_tests = False
        # if self.build_dataset_for_tests:  # todo rewrite
        #     self.set_testing_values()

        # if self.build_dataset_for_tests:
        #     '''filter down to one of every space group'''
        #     sg_representatives = {}
        #     for i in range(1, 231):
        #         print(f'Searching for example of spacegroup {i}')
        #         examples = np.where(dataset['crystal_space_group number'] == i)
        #         if len(examples[0]) > 0:
        #             sg_representatives[i] = examples[0][0]
        #
        #     dataset = dataset.loc[sg_representatives.values()]  # reduce to minimal examples
        #     if 'level_0' in dataset.columns:  # housekeeping
        #         dataset = dataset.drop(columns='level_0')
        #     dataset = dataset.reset_index()

        # if self.build_dataset_for_tests:
        #     '''save dataset and dataDims'''
        #     collater = Collater(None, None)
        #     crystaldata = collater(self.datapoints)
        #     torch.save(crystaldata, r'C:\Users\mikem\OneDrive\NYU\CSD\MCryGAN\tests/dataset_for_tests')
        #
        #     dataDims = self.get_dimension()
        #     torch.save(dataDims, r'C:\Users\mikem\OneDrive\NYU\CSD\MCryGAN\tests/dataset_for_tests_dataDims')
        #

    def set_crystal_generation_keys(self, dataset):
        # add symmetry features for generator
        self.crystal_generation_features = []
        space_group_features = [column for column in dataset.columns if 'sg_is' in column]
        crystal_system_features = [column for column in dataset.columns if 'crystal_system_is' in column]
        self.crystal_generation_features.extend(space_group_features)
        self.crystal_generation_features.extend(crystal_system_features)
        self.crystal_generation_features.append('crystal_z_value')
        self.crystal_generation_features.append('crystal_z_prime')
        self.crystal_generation_features.append('crystal_symmetry_multiplicity')
        self.crystal_generation_features.append('crystal_packing_coefficient')
        self.crystal_generation_features.append('crystal_cell_volume')
        self.crystal_generation_features.append('crystal_reduced_volume')

    def set_tracking_keys(self):
        """
        set keys to be kept in tracking feature array
        will break if any of these are objects or strings
        """
        self.tracking_keys = []
        self.tracking_keys.extend(self.crystal_keys)
        self.tracking_keys.extend(self.lattice_keys)
        self.tracking_keys.extend(self.molecule_keys)
        self.tracking_keys = list(set(self.tracking_keys))  # remove duplicates

        # 
        # space_group_features = [column for column in dataset.columns if 'sg_is' in column]
        # crystal_system_features = [column for column in dataset.columns if 'crystal_system_is' in column]
        # keys_to_add.extend(space_group_features)
        # keys_to_add.extend(crystal_system_features)
        # keys_to_add.extend(self.crystal_keys)
        # 
        # 
        # for key in dataset.keys():
        #     if ('molecule' in key) and ('fraction' in key):
        #         keys_to_add.append(key)
        #     if 'molecule_has' in key:
        #         keys_to_add.append(key)

    def set_crystal_keys(self):
        self.crystal_keys = ['crystal_space_group_number', 'crystal_space_group_setting',
                             'crystal_calculated_density', 'crystal_packing_coefficient',
                             'crystal_lattice_centring', 'crystal_system',
                             'crystal_lattice_alpha', 'crystal_lattice_beta', 'crystal_lattice_gamma',
                             'crystal_lattice_a', 'crystal_lattice_b', 'crystal_lattice_c',
                             'crystal_z_value', 'crystal_z_prime', 'crystal_reduced_volume',
                             'crystal_symmetry_multiplicity', 'asymmetric_unit_handedness', 'asymmetric_unit_is_well_defined',
                             ]

        self.lattice_keys = ['crystal_lattice_alpha', 'crystal_lattice_beta', 'crystal_lattice_gamma',
                             'crystal_lattice_a', 'crystal_lattice_b', 'crystal_lattice_c',
                             'asymmetric_unit_centroid_x', 'asymmetric_unit_centroid_y', 'asymmetric_unit_centroid_z',
                             'asymmetric_unit_rotvec_theta', 'asymmetric_unit_rotvec_phi', 'asymmetric_unit_rotvec_r'
                             ]

    def last_minute_featurization_and_one_hots(self, dataset, config):
        """
        add or update a few features including crystal feature one-hots
        #
        note we are surpressing a performancewarning from Pandas here.
        It's easier to do it this way and doesn't seem that slow.
        """

        '''
        z_value
        '''
        for i in range(1, np.amax(dataset['crystal_z_value']) + 1):
            dataset['crystal_z_is_{}'.format(i)] = dataset['crystal_z_value'] == i

        '''
        space group
        '''
        for i, symbol in enumerate(np.unique(list(self.sg_dict.values()))):
            dataset['crystal_sg_is_' + symbol] = dataset['crystal_space_group symbol'] == symbol

        '''
        crystal system
        '''
        # get dictionary for crystal system elements
        for i, system in enumerate(np.unique(list(self.lattice_dict.values()))):
            dataset['crystal_system_is_' + system] = dataset['crystal_system'] == system

        '''
        # set angle units to natural
        '''
        if (max(dataset['crystal_lattice_alpha']) > np.pi) or (max(dataset['crystal_lattice_beta']) > np.pi) or (max(dataset['crystal_lattice_gamma']) > np.pi):
            dataset['crystal_lattice_alpha'] = dataset['crystal_lattice_alpha'] * np.pi / 180
            dataset['crystal_lattice_beta'] = dataset['crystal_lattice_beta'] * np.pi / 180
            dataset['crystal_lattice_gamma'] = dataset['crystal_lattice_gamma'] * np.pi / 180

        '''
        check for heavy atoms
        '''
        znums = [10, 18, 36, 54]
        for znum in znums:
            dataset[f'molecule_atom_heavier_than_{znum}_fraction'] = np.asarray([get_range_fraction(atom_list, [znum, 200]) for atom_list in dataset['atom Z']])

        return dataset

    def shuffle_datapoints(self):
        good_inds = np.random.choice(self.dataset_length, size=self.dataset_length, replace=False)
        self.dataset_length = len(good_inds)
        self.datapoints = [self.datapoints[i] for i in good_inds]

    def generate_training_data(self, atom_coords, smiles, atom_features_list, mol_features,
                               targets, tracking_features, reference_cells, lattice_features,
                               T_fc_list, identifiers, asymmetric_unit_handedness, crystal_symmetries):
        """
        convert feature, target and tracking vectors into torch.geometric data objects
        :param atom_coords:
        :param smiles:
        :param atom_features_list:
        :param mol_features:
        :param targets:
        :param tracking_features:
        :return:
        """
        datapoints = []

        mult_ind = self.tracking_keys.index('crystal_symmetry_multiplicity')
        sg_ind_value_ind = self.tracking_keys.index('crystal_space_group_number')
        mol_size_ind = self.tracking_keys.index('molecule_num_atoms')
        mol_volume_ind = self.tracking_keys.index('molecule_volume')

        tracking_features = torch.Tensor(tracking_features)
        print("Generating crystal data objects")
        for i in tqdm.tqdm(range(self.dataset_length)):
            datapoints.append(
                CrystalData(x=torch.Tensor(atom_features_list[i]),
                            pos=torch.Tensor(atom_coords[i]),
                            y=targets[i],
                            mol_x=torch.Tensor(mol_features[i, None, :]),
                            smiles=smiles[i],
                            tracking=tracking_features[i, None, :],
                            ref_cell_pos=reference_cells[i][:, :, :3],  # won't collate properly as a torch tensor
                            mult=tracking_features[i, mult_ind].int(),
                            sg_ind=tracking_features[i, sg_ind_value_ind].int(),
                            cell_params=torch.Tensor(lattice_features[i, None, :]),
                            T_fc=torch.Tensor(T_fc_list[i])[None, ...],
                            mol_size=torch.Tensor(tracking_features[i, mol_size_ind]),
                            mol_volume=torch.Tensor(tracking_features[i, mol_volume_ind]),
                            csd_identifier=identifiers[i],
                            asym_unit_handedness=torch.Tensor(np.asarray(asymmetric_unit_handedness[i])[None]),
                            symmetry_operators=crystal_symmetries[i]
                            ))

        return datapoints

    def concatenate_atom_features(self, dataset):
        """
        collect and normalize/standardize relevant atomic features
        must be bools ints or floats
        :param dataset:
        :return:
        """
        atom_features_list = [np.zeros((len(dataset['atom_atomic_numbers'][i]), len(self.atom_keys))) for i in range(self.dataset_length)]

        for column_ind, key in enumerate(self.atom_keys):
            for i in range(self.dataset_length):
                feature_vector = dataset[key][i]

                if type(feature_vector) is not np.ndarray:
                    feature_vector = np.asarray(feature_vector)

                if key == 'atom Z':
                    pass
                elif feature_vector.dtype == bool:
                    pass
                else:
                    feature_vector = standardize(feature_vector, known_mean=self.std_dict[key][0], known_std=self.std_dict[key][1])

                assert np.sum(np.isnan(feature_vector)) == 0
                atom_features_list[i][:, column_ind] = feature_vector

        return atom_features_list

    def concatenate_molecule_features(self, dataset):
        """
        collect features of 'molecules' and append to atom-level data
        """

        # don't add molecule target if we are going to model it
        if self.regression_target in self.molecule_keys:
            self.molecule_keys.remove(self.regression_target)

        molecule_feature_array = np.zeros((self.dataset_length, len(self.molecule_keys)), dtype=float)
        for column_ind, key in enumerate(self.molecule_keys):
            feature_vector = dataset[key]

            if type(feature_vector) is not np.ndarray:
                feature_vector = np.asarray(feature_vector)

            if feature_vector.dtype == bool:
                pass
            else:
                feature_vector = standardize(feature_vector, known_mean=self.std_dict[key][0], known_std=self.std_dict[key][1])

            molecule_feature_array[:, column_ind] = feature_vector

        assert np.sum(np.isnan(molecule_feature_array)) == 0
        return molecule_feature_array

    def generate_training_datapoints(self, dataset):
        lattice_features = self.get_cell_features(dataset)
        targets = self.get_regression_target(dataset)
        tracking_features = self.gather_tracking_features(dataset)
        molecule_features_array = self.concatenate_molecule_features(dataset)
        atom_features_list = self.concatenate_atom_features(dataset)

        return self.generate_training_data(atom_coords=dataset['atom_coordinates'],
                                           smiles=dataset['molecule_smiles'],
                                           atom_features_list=atom_features_list,
                                           mol_features=molecule_features_array,
                                           targets=targets,
                                           tracking_features=tracking_features,
                                           reference_cells=dataset['unit_cell_coordinates'],
                                           lattice_features=lattice_features,
                                           T_fc_list=dataset['crystal_fc_transform'],
                                           identifiers=dataset['crystal_identifier'],
                                           asymmetric_unit_handedness=dataset['asymmetric_unit_handedness'],
                                           crystal_symmetries=dataset['crystal_symmetry_operators'])

    def get_cell_features(self, dataset):
        key_dtype = []
        # featurize

        feature_array = np.zeros((self.dataset_length, len(self.lattice_keys)), dtype=float)
        for column_ind, key in enumerate(self.lattice_keys):
            feature_vector = dataset[key]

            if type(feature_vector) is not np.ndarray:
                feature_vector = np.asarray(feature_vector)

            if key == 'crystal_z_value':
                key_dtype.append('int32')
            else:
                key_dtype.append(feature_vector.dtype)

            feature_array[:, column_ind] = feature_vector

            assert np.sum(np.isnan(feature_vector)) == 0

        '''
        compute full covariance matrix, in normalized basis
        '''
        # normalize the cell lengths against molecule volume & z_value
        normed_cell_lengths = feature_array[:, :3] / (dataset['crystal_z_value'].to_numpy()[:, None] ** (1 / 3)) / (dataset['molecule_volume'].to_numpy()[:, None] ** (1 / 3))
        feature_array_with_normed_lengths = feature_array.copy()
        feature_array_with_normed_lengths[:, :3] = normed_cell_lengths

        if len(feature_array_with_normed_lengths) == 1:  # error handling for if there_is_only one entry in the dataset, e.g., during CSP
            feature_array_with_normed_lengths = np.stack([feature_array_with_normed_lengths for _ in range(10)])[:, 0, :]
        self.covariance_matrix = np.cov(feature_array_with_normed_lengths, rowvar=False)  # we want the randn model to generate samples with normed lengths

        for i in range(len(self.covariance_matrix)):  # ensure it's well-conditioned
            self.covariance_matrix[i, i] = max((0.01, self.covariance_matrix[i, i]))

        return feature_array

    def get_regression_target(self, dataset):
        targets = dataset[self.regression_target]
        target_mean = self.std_dict[self.regression_target[0]]
        target_std = self.std_dict[self.regression_target[1]]

        return (targets - target_mean) / target_std

    def gather_tracking_features(self, dataset):
        """
        collect features of 'molecules' and append to atom-level data
        these must all be bools ints or floats - no strings will be processed
        """
        feature_array = np.zeros((self.dataset_length, len(self.tracking_keys)), dtype=float)
        for column_ind, key in enumerate(self.tracking_keys):
            feature_vector = dataset[key]

            if type(feature_vector) is not np.ndarray:
                feature_vector = np.asarray(feature_vector)

            feature_array[:, column_ind] = feature_vector

        return feature_array

    def get_dimension(self):
        dim = {
            'standardization_dict': self.std_dict,
            'dataset_length': self.dataset_length,

            'lattice_features': self.lattice_keys,
            'num lattice_features': len(self.lattice_keys),
            'lattice_means': np.asarray([self.std_dict[key][0] for key in self.lattice_keys]),
            'lattice_stds': np.asarray([self.std_dict[key][1] for key in self.lattice_keys]),
            'lattice cov mat': self.covariance_matrix,

            'regression_target': self.regression_target,
            'target_mean': self.std_dict[self.regression_target][0],
            'target_std': self.std_dict[self.regression_target][1],

            'num_tracking_features': len(self.tracking_keys),
            'tracking_features': self.tracking_keys,

            'num_atom_features': len(self.atom_keys),
            'atom_features': self.atom_keys,

            'num_mol_features': len(self.molecule_keys),
            'molecule_features': self.molecule_keys,

            'crystal_generation features': self.crystal_generation_features,
            'num crystal generation features': len(self.crystal_generation_features),

        }

        return dim

    def __getitem__(self, idx):
        return self.datapoints[idx]

    def __len__(self):
        return len(self.datapoints)


def get_dataloaders(dataset_builder, machine, batch_size, test_fraction=0.2):
    batch_size = batch_size
    train_size = int((1 - test_fraction) * len(dataset_builder))  # split data into training and test sets
    test_size = len(dataset_builder) - train_size

    train_dataset = []
    test_dataset = []

    for i in range(test_size, test_size + train_size):
        train_dataset.append(dataset_builder[i])
    for i in range(test_size):
        test_dataset.append(dataset_builder[i])

    if machine == 'cluster':  # faster dataloading on cluster with more workers
        if len(train_dataset) > 0:
            tr = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=min(os.cpu_count(), 8), pin_memory=True)
        else:
            tr = None
        te = DataLoader(test_dataset, batch_size=batch_size, shuffle=True, num_workers=min(os.cpu_count(), 8), pin_memory=True)
    else:
        if len(train_dataset) > 0:
            tr = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=0, pin_memory=True)
        else:
            tr = None
        te = DataLoader(test_dataset, batch_size=batch_size, shuffle=True, num_workers=0, pin_memory=True)

    return tr, te


def update_dataloader_batch_size(loader, new_batch_size):
    return DataLoader(loader.dataset, batch_size=new_batch_size, shuffle=True, num_workers=loader.num_workers, pin_memory=loader.pin_memory)

#
# def get_extra_test_loader(config, paths, dataDims, pg_dict=None, sg_dict=None, lattice_dict=None, sym_ops_dict=None):
#     # todo rewrite
#     datasets = []
#     for path in paths:
#         miner = DataManager(config=config, dataset_path=path, collect_chunks=False)
#         # miner.include_sgs = None # this will allow all sg's, which we can't do currently due to ongoing asymmetric unit parameterization
#         miner.exclude_nonstandard_settings = False
#         miner.exclude_crystal_systems = None
#         miner.exclude_polymorphs = False
#         miner.exclude_missing_r_factor = False
#         miner.exclude_blind_test_targets = False
#         dataset_i = miner.load_for_modelling(save_dataset=False, return_dataset=True)
#
#         if config.test_mode:
#             np.random.seed(config.seeds.dataset)
#             randinds = np.random.choice(len(dataset_i), min(len(dataset_i), 500), replace=False)
#             dataset_i = dataset_i.loc[randinds]
#
#         datasets.append(dataset_i)
#         del miner, dataset_i
#
#     dataset = pd.concat(datasets)
#     if 'level_0' in dataset.columns:  # housekeeping
#         dataset = dataset.drop(columns='level_0')
#     dataset = dataset.reset_index()
#
#     print('Fixing BT submission symmetries - fix this in next refeaturization')
#     # dataset = dataset.drop('crystal_symmetries', axis=1)  # can't mix nicely # todo fix this after next BT refeaturization - included sym ops are garbage
#     dataset['crystal_symmetries'] = [
#         sym_ops_dict[dataset['crystal_space_group number'][ii]] for ii in range(len(dataset))
#     ]
#     dataset['crystal_z_value'] = np.array(
#         [len(dataset['crystal_symmetries'][ii]) for ii in range(len(dataset))
#          ])
#
#     extra_test_set_builder = TrainingDataBuilder(config, pg_dict=pg_dict,
#                                                  sg_dict=sg_dict,
#                                                  lattice_dict=lattice_dict,
#                                                  replace_dataDims=dataDims,
#                                                  override_length=len(dataset),
#                                                  premade_dataset=dataset)
#
#     extra_test_loader = DataLoader(extra_test_set_builder.datapoints, batch_size=config.current_batch_size, shuffle=False, num_workers=0, pin_memory=False)
#     del dataset, extra_test_set_builder
#     return extra_test_loader
#
#
# def load_test_dataset(test_dataset_path: str): # todo rewrite
#     """
#     load dataset & useful info for test modules
#     testing dataset generated in BuildDataset with flag build_dataset_for_tests
#     """
#     '''load dataset'''
#     test_dataset = torch.load(test_dataset_path)
#
#     '''retrieve dataset statistics'''
#     dataDims = torch.load(test_dataset_path + '_dataDims')
#
#     '''load symmetry info'''
#     sym_info = np.load(r'C:\Users\mikem\OneDrive\NYU\CSD\MCryGAN\symmetry_info.npy', allow_pickle=True).item()
#     symmetry_info = {'sym_ops': sym_info['sym_ops'], 'point_groups': sym_info['point_groups'],
#                      'lattice_type': sym_info['lattice_type'], 'space_groups': sym_info['space_groups']}
#
#     return test_dataset, dataDims, symmetry_info
