import os
import time
from datetime import datetime
from argparse import Namespace
#
# os.environ['CUDA_LAUNCH_BLOCKING'] = "1" # slows down runtime

import sys

import torch
import torch.random
import wandb
from torch import backends
import torch.nn as nn
import numpy as np
import tqdm
from shutil import copy
from distutils.dir_util import copy_tree
from torch.nn import functional as F

from constants.atom_properties import VDW_RADII, ATOM_WEIGHTS
from constants.asymmetric_units import asym_unit_dict
from constants.space_group_info import (POINT_GROUPS, LATTICE_TYPE, SPACE_GROUPS, SYM_OPS)

from models.discriminator_models import crystal_discriminator
from models.generator_models import crystal_generator, independent_gaussian_model
from models.regression_models import molecule_regressor
from models.utils import (reload_model, init_schedulers, softmax_and_score, compute_packing_coefficient,
                          save_checkpoint, set_lr, cell_vol_torch, init_optimizer, get_regression_loss, compute_num_h_bonds)
from models.utils import (weight_reset, get_n_config)
from models.vdw_overlap import vdw_overlap
from models.crystal_rdf import crystal_rdf

from crystal_building.utils import (random_crystaldata_alignment, align_crystaldata_to_principal_axes,
                                    batch_molecule_principal_axes_torch, compute_Ip_handedness, clean_cell_params)
from crystal_building.builder import SupercellBuilder
from crystal_building.utils import update_crystal_symmetry_elements

from dataset_management.manager import DataManager
from dataset_management.modelling_utils import (TrainingDataBuilder, get_dataloaders, update_dataloader_batch_size)
from reporting.logger import Logger

from csp.utils import log_csp_summary_stats, compute_csp_sample_distances, plot_mini_csp_dist_vs_score, sample_density_funnel_plot, sample_rdf_funnel_plot
from reporting.csp.utils import log_mini_csp_scores_distributions, log_csp_cell_params
from common.utils import np_softmax
from torch_geometric.loader.dataloader import Collater


# https://www.ruppweb.org/Xray/tutorial/enantio.htm non enantiogenic groups
# https://dictionary.iucr.org/Sohncke_groups#:~:text=Sohncke%20groups%20are%20the%20three,in%20the%20chiral%20space%20groups.


class Modeller:
    def __init__(self, config):
        self.config = config
        self.device = self.config.device
        if self.config.device == 'cuda':
            backends.cudnn.benchmark = True  # auto-optimizes certain backend processes

        self.packing_loss_coefficient = 1
        '''get some physical constants'''
        self.atom_weights = ATOM_WEIGHTS
        self.vdw_radii = VDW_RADII
        self.sym_ops = SYM_OPS
        self.point_groups = POINT_GROUPS
        self.lattice_type = LATTICE_TYPE
        self.space_groups = SPACE_GROUPS
        self.sym_info = {  # collect space group info into single dict
            'sym_ops': self.sym_ops,
            'point_groups': self.point_groups,
            'lattice_type': self.lattice_type,
            'space_groups': self.space_groups}

        '''set space groups to be included and generated'''
        if self.config.generate_sgs == 'all':
            self.config.generate_sgs = [self.space_groups[int(key)] for key in asym_unit_dict.keys()]

        '''prep workdir'''
        self.source_directory = os.getcwd()
        self.prep_new_working_directory()

        '''load dataset'''
        data_manager = DataManager(device=self.device,
                                   datasets_path=self.config.dataset_path
                                   )
        data_manager.load_dataset_for_modelling(
            dataset_name=self.config.dataset_name,
            filter_conditions=self.config.dataset.filter_conditions,
            filter_polymorphs=self.config.dataset.filter_polymorphs,
            filter_duplicate_molecules=self.config.dataset.filter_duplicate_molecules
        )
        self.prepped_dataset = data_manager.dataset
        self.std_dict = data_manager.standardization_dict
        del data_manager

        self.train_discriminator = (config.mode == 'gan') and any((config.discriminator.train_adversarially, config.discriminator.train_on_distorted, config.discriminator.train_on_randn))
        self.train_generator = (config.mode == 'gan') and any((config.generator.train_vdw, config.generator.train_adversarially, config.generator.train_h_bond))
        self.train_regressor = config.mode == 'regression'

    def prep_new_working_directory(self):
        self.make_sequential_directory()
        os.mkdir(self.working_directory + '/source')
        yaml_path = self.config.paths.yaml_path

        # copy source to workdir for record keeping purposes
        copy_tree("common", self.working_directory + "/source/common")
        copy_tree("crystal_building", self.working_directory + "/source/crystal_building")
        copy_tree("dataset_management", self.working_directory + "/source/dataset_management")
        copy_tree("models", self.working_directory + "/source/models")
        copy_tree("reporting", self.working_directory + "/source/reporting")
        copy_tree("sampling", self.working_directory + "/source/sampling")
        copy("crystal_modeller.py", self.working_directory + "/source")
        copy("main.py", self.working_directory + "/source")
        np.save(self.working_directory + '/run_config', self.config)
        os.chdir(self.working_directory)  # move to working dir
        copy(yaml_path, os.getcwd())  # copy full config for reference
        print('Starting fresh run ' + self.working_directory)

    def make_sequential_directory(self):  # make working directory
        """
        make a new working directory
        non-overlapping previous entries
        or with a preset number
        :return:
        """
        self.working_directory = self.config.workdir + datetime.today().strftime("%d-%m-%H-%M-%S")
        os.mkdir(self.working_directory)

    def init_models(self):
        """
        Initialize models and optimizers and schedulers
        :return:
        """
        self.config = self.reload_model_checkpoints(self.config)

        self.generator, self.discriminator, self.regressor = [nn.Linear(1, 1) for _ in range(3)]
        print("Initializing model(s) for " + self.config.mode)
        if self.config.mode == 'gan' or self.config.mode == 'sampling' or self.config.mode == 'embedding':
            self.generator = crystal_generator(self.config.seeds.model, self.device, self.config.generator.model, self.dataDims, self.sym_info)
            self.discriminator = crystal_discriminator(self.config.seeds.model, self.config.discriminator.model, self.dataDims)
        if self.config.mode == 'regression' or self.config.regressor_path is not None:
            self.regressor = molecule_regressor(self.config.seeds.model, self.config.regressor.model, self.dataDims)

        if self.config.device.lower() == 'cuda':
            print('Putting models on CUDA')
            torch.backends.cudnn.benchmark = True
            torch.cuda.empty_cache()
            self.generator = self.generator.cuda()
            self.discriminator = self.discriminator.cuda()
            self.regressor = self.regressor.cuda()

        self.generator_optimizer = init_optimizer(self.config.generator.optimizer, self.generator)
        self.discriminator_optimizer = init_optimizer(self.config.discriminator.optimizer, self.discriminator)
        self.regressor_optimizer = init_optimizer(self.config.regressor.optimizer, self.regressor)

        if self.config.generator_path is not None and (self.config.mode == 'gan' or self.config.mode == 'embedding'):
            self.generator, generator_optimizer = reload_model(self.generator, self.generator_optimizer,
                                                               self.config.generator_path)
        if self.config.discriminator_path is not None and (self.config.mode == 'gan' or self.config.mode == 'embedding'):
            self.discriminator, discriminator_optimizer = reload_model(self.discriminator, self.discriminator_optimizer,
                                                                       self.config.discriminator_path)
        if self.config.regressor_path is not None:
            self.regressor, regressor_optimizer = reload_model(self.regressor, self.regressor_optimizer,
                                                               self.config.regressor_path)

        self.generator_schedulers = init_schedulers(self.config.generator.optimizer, self.generator_optimizer)
        self.discriminator_schedulers = init_schedulers(self.config.discriminator.optimizer, self.discriminator_optimizer)
        self.regressor_schedulers = init_schedulers(self.config.regressor.optimizer, self.regressor_optimizer)

        num_params = [get_n_config(model) for model in [self.generator, self.discriminator, self.regressor]]
        print('Generator model has {:.3f} million or {} parameters'.format(num_params[0] / 1e6, int(num_params[0])))
        print('Discriminator model has {:.3f} million or {} parameters'.format(num_params[1] / 1e6, int(num_params[1])))
        print('Regressor model has {:.3f} million or {} parameters'.format(num_params[2] / 1e6, int(num_params[2])))

        wandb.watch((self.generator, self.discriminator, self.regressor), log_graph=True, log_freq=100)
        wandb.log({"Model Num Parameters": np.sum(np.asarray(num_params)),
                   "Initial Batch Size": self.config.current_batch_size})

    def prep_dataloaders(self, dataset_builder, test_fraction=0.2, override_batch_size: int = None):
        if override_batch_size is None:
            loader_batch_size = self.config.min_batch_size
        else:
            loader_batch_size = override_batch_size
        train_loader, test_loader = get_dataloaders(dataset_builder,
                                                    machine=self.config.machine,
                                                    batch_size=loader_batch_size,
                                                    test_fraction=test_fraction)
        self.config.current_batch_size = self.config.min_batch_size
        print("Training batch size set to {}".format(self.config.current_batch_size))
        del dataset_builder

        #  # todo rewrite this
        extra_test_loader = None  # data_loader for a secondary test set - analysis is hardcoded for CSD Blind Tests 5 & 6
        # if self.config.extra_test_set_paths is not None:
        #     extra_test_loader = get_extra_test_loader(self.config,
        #                                               self.config.extra_test_set_paths,
        #                                               dataDims=self.dataDims,
        #                                               pg_dict=self.point_groups,
        #                                               sg_dict=self.space_groups,
        #                                               lattice_dict=self.lattice_type,
        #                                               sym_ops_dict=self.sym_ops)

        return train_loader, test_loader, extra_test_loader

    # todo rewrite embedding analysis
    #
    # def crystal_embedding_analysis(self):
    #     """
    #     analyze the embeddings of a given crystal dataset
    #     embeddings provided by pretrained model
    #     """
    #     """
    #             train and/or evaluate one or more models
    #             regressor
    #             GAN (generator and/or discriminator)
    #             """
    #     with wandb.init(config=self.config,
    #                     project=self.config.wandb.project_name,
    #                     entity=self.config.wandb.username,
    #                     tags=[self.config.logger.experiment_tag],
    #                     settings=wandb.Settings(code_dir=".")):
    #
    #         wandb.run.name = wandb.config.machine + '_' + str(self.config.mode) + '_' + str(wandb.config.run_num)  # overwrite procedurally generated run name with our run name
    #
    #         '''miscellaneous setup'''
    #         dataset_builder = self.misc_pre_training_items()
    #
    #         '''prep dataloaders'''
    #         from torch_geometric.loader import DataLoader
    #         test_dataset = []
    #         for i in range(len(dataset_builder)):
    #             test_dataset.append(dataset_builder[i])
    #
    #         self.config.current_batch_size = self.config.min_batch_size
    #         print("Training batch size set to {}".format(self.config.current_batch_size))
    #         del dataset_builder
    #         test_loader = DataLoader(test_dataset, batch_size=self.config.current_batch_size, shuffle=True, num_workers=0, pin_memory=True)
    #
    #         '''instantiate models'''
    #         self.init_models()
    #
    #         '''initialize some training metrics'''
    #         with torch.autograd.set_detect_anomaly(self.config.anomaly_detection):
    #             # very cool
    #             print("  .--.      .-'.      .--.      .--.      .--.      .--.      .`-.      .--.")
    #             print(":::::.\::::::::.\::::::::.\::::::::.\::::::::.\::::::::.\::::::::.\::::::::.")
    #             print("'      `--'      `.-'      `--'      `--'      `--'      `-.'      `--'      `")
    #             # very cool
    #             print("Starting Embedding Analysis")
    #
    #             with torch.no_grad():
    #                 # compute test loss & save evaluation statistics on test samples
    #                 embedding_dict = self.embed_dataset(
    #                     data_loader=test_loader, generator=generator, discriminator=discriminator, regressor=regressor)
    #
    #                 np.save('embedding_dict', embedding_dict) #

    def embed_dataset(self, data_loader):
        t0 = time.time()
        discriminator.eval()

        embedding_dict = {
            'tracking_features': [],
            'identifiers': [],
            'scores': [],
            'source': [],
            'final_activation': [],
        }

        for i, data in enumerate(tqdm.tqdm(data_loader)):
            '''
            get discriminator embeddings
            '''

            '''real data'''
            real_supercell_data = self.supercell_builder.prebuilt_unit_cell_to_supercell(
                data, self.config.supercell_size, self.config.discriminator.model.convolution_cutoff)

            score_on_real, real_distances_dict, latent = \
                self.adversarial_score(real_supercell_data, return_latent=True)

            embedding_dict['tracking_features'].extend(data.tracking.cpu().detach().numpy())
            embedding_dict['identifiers'].extend(data.csd_identifier)
            embedding_dict['scores'].extend(score_on_real.cpu().detach().numpy())
            embedding_dict['final_activation'].extend(latent)
            embedding_dict['source'].extend(['real' for _ in range(len(latent))])

            '''fake data'''
            for j in tqdm.tqdm(range(100)):
                real_data = data.clone()
                generated_samples_i, negative_type, real_data = \
                    self.generate_discriminator_negatives(real_data, i, override_randn=True, override_distorted=True)

                fake_supercell_data, generated_cell_volumes, _ = self.supercell_builder.build_supercells(
                    real_data, generated_samples_i, self.config.supercell_size,
                    self.config.discriminator.model.convolution_cutoff,
                    align_molecules=(negative_type != 'generated'),
                    target_handedness=real_data.asym_unit_handedness,
                )

                score_on_fake, fake_pairwise_distances_dict, fake_latent = self.adversarial_score(fake_supercell_data, return_latent=True)

                embedding_dict['tracking_features'].extend(real_data.tracking.cpu().detach().numpy())
                embedding_dict['identifiers'].extend(real_data.csd_identifier)
                embedding_dict['scores'].extend(score_on_fake.cpu().detach().numpy())
                embedding_dict['final_activation'].extend(fake_latent)
                embedding_dict['source'].extend([negative_type for _ in range(len(latent))])

        embedding_dict['scores'] = np.stack(embedding_dict['scores'])
        embedding_dict['tracking_features'] = np.stack(embedding_dict['tracking_features'])
        embedding_dict['final_activation'] = np.stack(embedding_dict['final_activation'])

        total_time = time.time() - t0
        print(f"Embedding took {total_time:.1f} Seconds")

        '''distance matrix'''
        scores = softmax_and_score(embedding_dict['scores'])
        latents = torch.Tensor(embedding_dict['final_activation'])
        overlaps = torch.inner(latents, latents) / torch.outer(torch.linalg.norm(latents, dim=-1), torch.linalg.norm(latents, dim=-1))
        distmat = torch.cdist(latents, latents)

        sample_types = list(set(embedding_dict['source']))
        inds_dict = {}
        for source in sample_types:
            inds_dict[source] = np.argwhere(np.asarray(embedding_dict['source']) == source)[:, 0]

        mean_overlap_to_real = {}
        mean_dist_to_real = {}
        mean_score = {}
        for source in sample_types:
            sample_dists = distmat[inds_dict[source]]
            sample_scores = scores[inds_dict[source]]
            sample_overlaps = overlaps[inds_dict[source]]

            mean_dist_to_real[source] = sample_dists[:, inds_dict['real']].mean()
            mean_overlap_to_real[source] = sample_overlaps[:, inds_dict['real']].mean()
            mean_score[source] = sample_scores.mean()

        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        from plotly.colors import n_colors

        # '''distances'''
        # fig = make_subplots(rows=1, cols=2, subplot_titles=('distances', 'dot overlaps'))
        # fig.add_trace(go.Heatmap(z=distmat), row=1, col=1)
        # fig.add_trace(go.Heatmap(z=overlaps), row=1, col=2)
        # fig.show()

        '''distance to real vs score'''
        colors = n_colors('rgb(250,0,5)', 'rgb(5,150,250)', len(inds_dict.keys()), colortype='rgb')

        fig = make_subplots(rows=1, cols=2)
        for ii, source in enumerate(sample_types):
            fig.add_trace(go.Scattergl(
                x=distmat[inds_dict[source]][:, inds_dict['real']].mean(-1), y=scores[inds_dict[source]],
                mode='markers', marker=dict(color=colors[ii]), name=source), row=1, col=1
            )

            fig.add_trace(go.Scattergl(
                x=overlaps[inds_dict[source]][:, inds_dict['real']].mean(-1), y=scores[inds_dict[source]],
                mode='markers', marker=dict(color=colors[ii]), showlegend=False), row=1, col=2
            )

        fig.update_xaxes(title_text='mean distance to real', row=1, col=1)
        fig.update_yaxes(title_text='discriminator score', row=1, col=1)

        fig.update_xaxes(title_text='mean overlap to real', row=1, col=2)
        fig.update_yaxes(title_text='discriminator score', row=1, col=2)
        fig.show()

        return embedding_dict

    #
    # def prep_standalone_modelling_tools(self, batch_size, machine='local'):
    #     """
    #     to pass tools to another training pipeline
    #     """  # todo rewrite this and all standalone tools
    #     '''miscellaneous setup'''
    #     if machine == 'local':
    #         std_dataDims_path = '/home/mkilgour/mcrygan/old_dataset_management/standard_dataDims.npy'
    #     elif machine == 'cluster':
    #         std_dataDims_path = '/scratch/mk8347/mcrygan/old_dataset_management/standard_dataDims.npy'
    #
    #     standard_dataDims = np.load(std_dataDims_path, allow_pickle=True).item()  # maintain constant standardizations between runs
    #
    #     '''note this standard datadims construction will only work between runs with
    #     identical choice of features - there is a flag for this in the datasetbuilder'''
    #     dataset_builder = TrainingDataBuilder(self.config.dataset,
    #                                           preloaded_dataset=self.prepped_dataset,
    #                                           data_std_dict=self.std_dict,
    #                                           override_length=self.config.dataset.max_dataset_length)
    #
    #     self.dataDims = dataset_builder.dataDims
    #     del self.prepped_dataset  # we don't actually want this huge thing floating around
    #
    #     train_loader, test_loader, extra_test_loader = (
    #         self.prep_dataloaders(dataset_builder, test_fraction=0.2, override_batch_size=batch_size))
    #
    #     return train_loader, test_loader

    def train_crystal_models(self):
        """
        train and/or evaluate one or more models
        regressor
        GAN (generator and/or discriminator)
        """
        with ((wandb.init(config=self.config,
                          project=self.config.wandb.project_name,
                          entity=self.config.wandb.username,
                          tags=[self.config.logger.experiment_tag],
                          settings=wandb.Settings(code_dir=".")))):

            wandb.run.name = self.config.machine + '_' + self.config.mode + '_' + self.config.logger.run_name  # overwrite procedurally generated run name with our run name
            # config = wandb.config # wandb configs don't support nested namespaces. look at the github thread to see if they eventually fix it
            # this means we also can't do wandb sweeps properly, as-is

            '''miscellaneous setup'''
            dataset_builder = self.misc_pre_training_items()
            self.logger = Logger(self.config, self.dataDims, wandb)

            '''prep dataloaders'''
            train_loader, test_loader, extra_test_loader = \
                self.prep_dataloaders(dataset_builder, test_fraction=self.config.dataset.test_fraction)

            '''instantiate models'''
            self.init_models()

            '''initialize some training metrics'''
            self.discriminator_hit_max_lr, self.generator_hit_max_lr, self.regressor_hit_max_lr, converged, epoch = \
                (False, False, False, self.config.max_epochs == 0, 0)

            # training loop
            with torch.autograd.set_detect_anomaly(self.config.anomaly_detection):
                while (epoch < self.config.max_epochs) and not converged:
                    # very cool
                    print("⋅.˳˳.⋅ॱ˙˙ॱ⋅.˳˳.⋅ॱ˙˙ॱᐧ.˳˳.⋅⋅.˳˳.⋅ॱ˙˙ॱ⋅.˳˳.⋅ॱ˙˙ॱᐧ.˳˳.⋅⋅.˳˳.⋅ॱ˙˙ॱ⋅.˳˳.⋅ॱ˙˙ॱᐧ.˳˳.⋅⋅.˳˳.⋅ॱ˙˙ॱ⋅.˳˳.⋅ॱ˙˙ॱᐧ.˳˳.⋅")
                    # very cool
                    print("Starting Epoch {}".format(epoch))  # index from 0
                    self.logger.reset_for_new_epoch(epoch, test_loader.batch_size)

                    try:  # try this batch size
                        self.run_epoch(epoch_type='train', data_loader=train_loader,
                                       update_gradients=True, record_stats=True, epoch=epoch)

                        with torch.no_grad():
                            self.run_epoch(epoch_type='test', data_loader=test_loader,
                                           update_gradients=False, record_stats=True, epoch=epoch)

                            if (extra_test_loader is not None) and (epoch % self.config.extra_test_period == 0):
                                self.run_epoch(epoch_type='extra', data_loader=extra_test_loader,
                                               update_gradients=False, record_stats=True, epoch=epoch)  # compute loss on test set

                        self.logger.numpyize_current_losses()
                        self.logger.update_loss_record()

                        '''update learning rates'''
                        self.update_lr()

                        '''save checkpoints'''
                        self.model_checkpointing(epoch)

                        '''check convergence status'''
                        generator_converged, discriminator_converged, regressor_converged = \
                            self.logger.check_model_convergence()

                        '''sometimes test the generator on a mini CSP problem'''
                        if (self.config.mode == 'gan') and (epoch % self.config.logger.mini_csp_frequency == 0) and \
                                self.train_generator and (epoch > 0):
                            pass # self.batch_csp(extra_test_loader if extra_test_loader is not None else test_loader)

                        '''record metrics and analysis'''
                        self.logger.log_training_metrics()
                        self.logger.log_epoch_analysis(test_loader)

                        if (generator_converged and discriminator_converged and regressor_converged) \
                                and (epoch > self.config.history + 2):
                            print('Training has converged!')
                            break

                        '''increment batch size'''
                        train_loader, test_loader, extra_test_loader = \
                            self.increment_batch_size(train_loader, test_loader, extra_test_loader)

                    except RuntimeError as e:  # if we do hit OOM, slash the batch size
                        if "CUDA out of memory" in str(e):
                            train_loader, test_loader = self.slash_batch(train_loader, test_loader, 0.05)  # shrink batch size
                            self.config.grow_batch_size = False  # stop growing the batch for the rest of the run
                        else:
                            raise e
                    epoch += 1

                    if self.config.device.lower() == 'cuda':
                        torch.cuda.empty_cache()  # clear GPU --- not clear this does anything

                if self.config.mode == 'gan':  # evaluation on test metrics
                    self.gan_evaluation(epoch, test_loader, extra_test_loader)

    def run_epoch(self, epoch_type, data_loader=None, update_gradients=True, iteration_override=None, record_stats=False, epoch=None):
        self.epoch_type = epoch_type
        if self.config.mode == 'gan':
            if self.config.regressor_path is not None:
                self.regressor.eval()  # just using this to suggest densities to the generator

            return self.gan_epoch(data_loader, update_gradients, iteration_override)

        elif self.config.mode == 'regression':
            return self.regression_epoch(data_loader, update_gradients, iteration_override)

    def regression_epoch(self, data_loader, update_gradients=True, iteration_override=None):

        if update_gradients:
            self.regressor.train(True)
        else:
            self.regressor.eval()

        stats_keys = ['regressor_prediction', 'regressor_target', 'tracking_features']

        for i, data in enumerate(tqdm.tqdm(data_loader, miniters=int(len(data_loader) / 25))):
            if self.config.regressor_positional_noise > 0:
                data.pos += torch.randn_like(data.pos) * self.config.regressor_positional_noise

            data = data.to(self.device)

            regression_losses_list, predictions, targets = get_regression_loss(self.regressor, data, self.dataDims['target_mean'], self.dataDims['target_std'])
            regression_loss = regression_losses_list.mean()

            if update_gradients:
                self.regressor_optimizer.zero_grad(set_to_none=True)  # reset gradients from previous passes
                regression_loss.backward()  # back-propagation
                self.regressor_optimizer.step()  # update parameters

            '''log losses and other tracking values'''
            self.logger.update_current_losses('regressor', self.epoch_type,
                                              regression_loss.cpu().detach().numpy(),
                                              regression_losses_list.cpu().detach().numpy())

            stats_values = [predictions, targets]
            self.logger.update_stats_dict(self.epoch_type, stats_keys, stats_values, mode='extend')
            self.logger.update_stats_dict(self.epoch_type, 'tracking_features', data.tracking.cpu().detach().numpy(), mode='append')

            if iteration_override is not None:
                if i >= iteration_override:
                    break  # stop training early - for debugging purposes

        self.logger.numpyize_stats_dict(self.epoch_type)

    def gan_epoch(self, data_loader=None, update_gradients=True,
                  iteration_override=None):
        t0 = time.time()

        if update_gradients:
            self.generator.train(True)
            self.discriminator.train(True)
        else:
            self.generator.eval()
            self.discriminator.eval()

        for i, data in enumerate(tqdm.tqdm(data_loader, miniters=int(len(data_loader) / 10), mininterval=30)):
            data = data.to(self.config.device)

            '''
            train discriminator
            '''
            skip_discriminator_step = self.decide_whether_to_skip_discriminator(i, self.logger.get_stat_dict(self.epoch_type))

            self.discriminator_step(data, i, update_gradients, skip_step=skip_discriminator_step)
            '''
            train_generator
            '''
            self.generator_step(data, i, update_gradients)
            '''
            record some stats
            '''
            self.logger.update_stats_dict(self.epoch_type, 'tracking_features', data.tracking.cpu().detach().numpy(), mode='append')
            self.logger.update_stats_dict(self.epoch_type, 'identifiers', data.csd_identifier, mode='extend')

            if iteration_override is not None:
                if i >= iteration_override:
                    break  # stop training early - for debugging purposes

        self.logger.numpyize_stats_dict(self.epoch_type)

    def decide_whether_to_skip_discriminator(self, i, epoch_stats_dict):
        # hold discriminator training when it's beating the generator

        skip_discriminator_step = False
        if (i == 0) and self.config.generator.train_adversarially:
            skip_discriminator_step = True  # do not train except by express permission of the below condition
        if i > 0 and self.config.discriminator.train_adversarially:  # must skip first step since there will be no fake score to compare against
            avg_generator_score = np_softmax(np.stack(epoch_stats_dict['discriminator_fake_score'])[np.argwhere(np.asarray(epoch_stats_dict['generator_sample_source']) == 0)[:, 0]])[:, 1].mean()
            if avg_generator_score < 0.5:
                skip_discriminator_step = True
        return skip_discriminator_step

    def discriminator_evaluation(self, data_loader=None, discriminator=None, iteration_override=None):  # todo write generator evaluation
        t0 = time.time()
        discriminator.eval()  # todo replace with logger

        epoch_stats_dict = {
            'tracking_features': [],
            'identifiers': [],
            'scores': [],
            'intermolecular rdf': [],
            'atomistic energy': [],
            'full rdf': [],
            'vdw penalty': [],
        }

        for i, data in enumerate(tqdm.tqdm(data_loader)):
            '''
            evaluate discriminator
            '''
            real_supercell_data = \
                self.supercell_builder.prebuilt_unit_cell_to_supercell(data, self.config.supercell_size, self.config.discriminator.model.convolution_cutoff)

            if self.config.device.lower() == 'cuda':  # redundant
                real_supercell_data = real_supercell_data.cuda()

            if self.config.test_mode or self.config.anomaly_detection:
                assert torch.sum(torch.isnan(real_supercell_data.x)) == 0, "NaN in training input"

            score_on_real, real_distances_dict = self.adversarial_score(discriminator, real_supercell_data)

            epoch_stats_dict['tracking_features'].extend(data.tracking.cpu().detach().numpy())
            epoch_stats_dict['identifiers'].extend(data.csd_identifier)  #
            epoch_stats_dict['scores'].extend(score_on_real.cpu().detach().numpy())

            epoch_stats_dict['vdw penalty'].extend(
                -vdw_overlap(self.vdw_radii,
                             crystaldata=real_supercell_data,
                             return_score_only=True
                             ).cpu().detach().numpy())

            if iteration_override is not None:
                if i >= iteration_override:
                    break  # stop training early - for debugging purposes

        epoch_stats_dict['scores'] = np.stack(epoch_stats_dict['scores'])
        epoch_stats_dict['tracking_features'] = np.stack(epoch_stats_dict['tracking_features'])
        # epoch_stats_dict['full rdf'] = np.stack(epoch_stats_dict['full rdf'])
        # epoch_stats_dict['intermolecular rdf'] = np.stack(epoch_stats_dict['intermolecular rdf'])
        epoch_stats_dict['vdw penalty'] = np.asarray(epoch_stats_dict['vdw penalty'])

        total_time = time.time() - t0

        return epoch_stats_dict, total_time

    def adversarial_score(self, data, return_latent=False):
        output, extra_outputs = self.discriminator(data.clone(), return_dists=True, return_latent=return_latent)  # reshape output from flat filters to channels * filters per channel
        if return_latent:
            return output, extra_outputs['dists_dict'], extra_outputs['final_activation']
        else:
            return output, extra_outputs['dists_dict']

    def discriminator_step(self, data, i, update_gradients, skip_step):

        if self.train_discriminator:
            score_on_real, score_on_fake, generated_samples, \
                real_dist_dict, fake_dist_dict, real_vdw_score, fake_vdw_score, \
                real_packing_coeffs, fake_packing_coeffs, generated_samples_i \
                = self.get_discriminator_losses(data, i)

            discriminator_scores = torch.cat((score_on_real, score_on_fake))
            discriminator_target = torch.cat((torch.ones_like(score_on_real[:, 0]), torch.zeros_like(score_on_fake[:, 0])))
            discriminator_losses = F.cross_entropy(discriminator_scores, discriminator_target.long(), reduction='none')  # works much better

            discriminator_loss = discriminator_losses.mean()

            self.logger.update_current_losses('discriminator', self.epoch_type,
                                              discriminator_loss.data.cpu().detach().numpy(),
                                              discriminator_losses.cpu().detach().numpy())

            if update_gradients and (not skip_step):
                self.discriminator_optimizer.zero_grad(set_to_none=True)  # reset gradients from previous passes
                torch.nn.utils.clip_grad_norm_(self.discriminator.parameters(),
                                               self.config.gradient_norm_clip)  # gradient clipping
                discriminator_loss.backward()  # back-propagation
                self.discriminator_optimizer.step()  # update parameters

            stats_keys = ['discriminator_real_score', 'discriminator_fake_score',
                          'real vdw penalty', 'fake_vdw_penalty',
                          'generated_cell_parameters', 'final_generated_cell_parameters',
                          'real_packing_coefficients', 'generated_packing_coefficients']
            stats_values = [score_on_real.cpu().detach().numpy(), score_on_fake.cpu().detach().numpy(),
                            -real_vdw_score.cpu().detach().numpy(), -fake_vdw_score.cpu().detach().numpy(),
                            generated_samples_i.cpu().detach().numpy(), generated_samples,
                            real_packing_coeffs.cpu().detach().numpy(), fake_packing_coeffs.cpu().detach().numpy()]
            self.logger.update_stats_dict(self.epoch_type, stats_keys, stats_values, mode='extend')

    def generator_step(self, data, i, update_gradients):
        if self.train_generator:
            discriminator_raw_output, generated_samples, packing_loss, packing_prediction, packing_target, \
                vdw_loss, vdw_score, generated_dist_dict, supercell_examples, similarity_penalty, h_bond_score = \
                self.get_generator_losses(data, i)

            generator_losses = self.aggregate_generator_losses(
                packing_loss, discriminator_raw_output, vdw_loss, vdw_score,
                similarity_penalty, packing_prediction, packing_target, h_bond_score)

            generator_loss = generator_losses.mean()
            self.logger.update_current_losses('generator', self.epoch_type,
                                              generator_loss.data.cpu().detach().numpy(),
                                              generator_losses.cpu().detach().numpy())

            if update_gradients:
                self.generator_optimizer.zero_grad(set_to_none=True)  # reset gradients from previous passes
                torch.nn.utils.clip_grad_norm_(self.generator.parameters(),
                                               self.config.gradient_norm_clip)  # gradient clipping
                generator_loss.backward()  # back-propagation
                self.generator_optimizer.step()  # update parameters

            self.logger.update_stats_dict(self.epoch_type, 'final_generated_cell_parameters',
                                          supercell_examples.cell_params.cpu().detach().numpy(), mode='extend')

            del supercell_examples

    def get_discriminator_losses(self, data, i):
        """
        generate real and fake crystals
        and score them
        """

        '''get real supercells'''
        real_supercell_data = self.supercell_builder.prebuilt_unit_cell_to_supercell(
            data, self.config.supercell_size, self.config.discriminator.model.convolution_cutoff)

        '''get fake supercells'''
        generated_samples_i, negative_type, generator_data = \
            self.generate_discriminator_negatives(data, i)

        fake_supercell_data, generated_cell_volumes, _ = self.supercell_builder.build_supercells(
            generator_data, generated_samples_i, self.config.supercell_size,
            self.config.discriminator.model.convolution_cutoff,
            align_molecules=(negative_type != 'generated'),
            target_handedness=generator_data.asym_unit_handedness,
        )

        '''apply noise'''
        if self.config.discriminator_positional_noise > 0:
            real_supercell_data.pos += \
                torch.randn_like(real_supercell_data.pos) * self.config.discriminator_positional_noise
            fake_supercell_data.pos += \
                torch.randn_like(fake_supercell_data.pos) * self.config.discriminator_positional_noise

        '''score'''
        score_on_real, real_distances_dict, real_latent = self.adversarial_score(real_supercell_data, return_latent=True)
        score_on_fake, fake_pairwise_distances_dict, fake_latent = self.adversarial_score(fake_supercell_data, return_latent=True)

        '''recompute packing coeffs'''
        real_packing_coeffs = compute_packing_coefficient(cell_params=real_supercell_data.cell_params,
                                                          mol_volumes=real_supercell_data.mol_volume,
                                                          z_values=real_supercell_data.mult)
        fake_packing_coeffs = compute_packing_coefficient(cell_params=fake_supercell_data.cell_params,
                                                          mol_volumes=fake_supercell_data.mol_volume,
                                                          z_values=fake_supercell_data.mult)

        return score_on_real, score_on_fake, fake_supercell_data.cell_params.cpu().detach().numpy(), \
            real_distances_dict, fake_pairwise_distances_dict, \
            vdw_overlap(self.vdw_radii, crystaldata=real_supercell_data, return_score_only=True), \
            vdw_overlap(self.vdw_radii, crystaldata=fake_supercell_data, return_score_only=True), \
            real_packing_coeffs, fake_packing_coeffs, \
            generated_samples_i

    def set_molecule_alignment(self, data, right_handed=False, mode_override=None):
        if mode_override is not None:
            mode = mode_override
        else:
            mode = self.config.generator.canonical_conformer_orientation

        if mode == 'standardized':
            data = align_crystaldata_to_principal_axes(data, handedness=data.asym_unit_handedness)
            # data.asym_unit_handedness = torch.ones_like(data.asym_unit_handedness)

        elif mode == 'random':
            data = random_crystaldata_alignment(data)
            if right_handed:
                coords_list = [data.pos[data.ptr[i]:data.ptr[i + 1]] for i in range(data.num_graphs)]
                coords_list_centred = [coords_list[i] - coords_list[i].mean(0) for i in range(data.num_graphs)]
                principal_axes_list, _, _ = batch_molecule_principal_axes_torch(coords_list_centred)
                handedness = compute_Ip_handedness(principal_axes_list)
                for ind, hand in enumerate(handedness):
                    if hand == -1:
                        data.pos[data.batch == ind] = -data.pos[data.batch == ind]  # invert

                data.asym_unit_handedness = torch.ones_like(data.asym_unit_handedness)

        return data

    def get_generator_samples(self, data, alignment_override=None):
        """

        """
        mol_data = data.clone()
        # conformer orientation setting
        mol_data = self.set_molecule_alignment(mol_data, mode_override=alignment_override)

        # noise injection
        if self.config.generator_positional_noise > 0:
            mol_data.pos += torch.randn_like(mol_data.pos) * self.config.generator_positional_noise

        # update symmetry information
        if self.config.generate_sgs is not None:
            mol_data = update_crystal_symmetry_elements(mol_data, self.config.generate_sgs, self.dataDims,
                                                        self.sym_info, randomize_sgs=True)

        # update packing coefficient
        if self.config.regressor_path is not None:
            # predict the crystal density and feed it as an input to the generator
            with torch.no_grad():
                standardized_target_packing_coeff = self.regressor(mol_data.clone().detach().to(self.config.device)).detach()[:, 0]
        else:
            target_packing_coeff = mol_data.tracking[:, self.dataDims['tracking_features'].index('crystal_packing_coefficient')]
            standardized_target_packing_coeff = ((target_packing_coeff - self.std_dict['crystal_packing_coefficient'][0]) / self.std_dict['crystal_packing_coefficient'][1]).to(self.config.device)

        standardized_target_packing_coeff += torch.randn_like(standardized_target_packing_coeff) * self.config.generator.packing_target_noise

        # generate the samples
        [generated_samples, prior, condition] = self.generator.forward(
            n_samples=mol_data.num_graphs, molecule_data=mol_data.to(self.config.device).clone(),
            return_condition=True, return_prior=True, target_packing=standardized_target_packing_coeff)

        return generated_samples, prior, standardized_target_packing_coeff, mol_data

    def get_generator_losses(self, data, i):
        """
        train the generator
        """
        """get crystals"""
        generated_samples, prior, standardized_target_packing, generator_data = (
            self.get_generator_samples(data))

        supercell_data, generated_cell_volumes, _ = (
            self.supercell_builder.build_supercells(
                generator_data, generated_samples, self.config.supercell_size,
                self.config.discriminator.model.convolution_cutoff,
                align_molecules=False
            ))

        """get losses"""
        similarity_penalty = self.compute_similarity_penalty(generated_samples, prior)
        discriminator_raw_output, dist_dict = self.score_adversarially(supercell_data)
        h_bond_score = self.compute_h_bond_score(supercell_data)
        vdw_loss, vdw_score, _, _ = vdw_overlap(self.vdw_radii,
                                                dist_dict=dist_dict,
                                                num_graphs=generator_data.num_graphs,
                                                graph_sizes=generator_data.mol_size,
                                                loss_func=self.config.generator.vdw_loss_func)
        packing_loss, packing_prediction, packing_target, packing_csd = \
            self.generator_density_matching_loss(
                standardized_target_packing, supercell_data, generated_samples,
                precomputed_volumes=generated_cell_volumes, loss_func=self.config.generator.density_loss_func)

        return discriminator_raw_output, generated_samples.cpu().detach().numpy(), \
            packing_loss, packing_prediction.cpu().detach().numpy(), \
            packing_target.cpu().detach().numpy(), \
            vdw_loss, vdw_score, dist_dict, \
            supercell_data, similarity_penalty, h_bond_score

    def misc_pre_training_items(self):
        """
        dataset_builder: for going from database to trainable dataset
        dataDims: contains key information about the dataset
        number of generators for discriminator training
        supercell_builder
        tracking indices for certain properties
        symmetry element indexing
        multivariate gaussian generator
        """

        dataset_builder = TrainingDataBuilder(self.config.dataset,
                                              preloaded_dataset=self.prepped_dataset,
                                              data_std_dict=self.std_dict,
                                              override_length=self.config.dataset.max_dataset_length)

        self.dataDims = dataset_builder.dataDims
        del self.prepped_dataset  # we don't actually want this huge thing floating around

        '''init lattice mean & std'''
        self.lattice_means = torch.tensor(self.dataDims['lattice_means'], dtype=torch.float32, device=self.config.device)
        self.lattice_stds = torch.tensor(self.dataDims['lattice_stds'], dtype=torch.float32, device=self.config.device)

        '''
        init supercell builder
        '''
        self.supercell_builder = SupercellBuilder(
            self.sym_info, self.dataDims, device=self.config.device, rotation_basis='spherical')

        ''' 
        init gaussian generator for cell parameter sampling
        we don't always use it but it's very cheap so just do it every time
        '''
        self.gaussian_generator = independent_gaussian_model(input_dim=self.dataDims['num_lattice_features'],
                                                             means=self.dataDims['lattice_means'],
                                                             stds=self.dataDims['lattice_stds'],
                                                             sym_info=self.sym_info,
                                                             device=self.config.device,
                                                             cov_mat=self.dataDims['lattice_cov_mat'])

        return dataset_builder

    def what_generators_to_use(self, override_randn, override_distorted, override_adversarial):
        n_generators = sum((self.config.discriminator.train_on_randn or override_randn,
                            self.config.discriminator.train_on_distorted or override_distorted,
                            self.config.discriminator.train_adversarially or override_adversarial))

        gen_randint = np.random.randint(0, n_generators, 1)

        generator_ind_list = []
        if self.config.discriminator.train_adversarially or override_adversarial:
            generator_ind_list.append(1)
        if self.config.discriminator.train_on_randn or override_randn:
            generator_ind_list.append(2)
        if self.config.discriminator.train_on_distorted or override_distorted:
            generator_ind_list.append(3)

        generator_ind = generator_ind_list[int(gen_randint)]  # randomly select which generator to use from the available set

        return n_generators, generator_ind

    def generate_discriminator_negatives(self, real_data, i, override_adversarial=False, override_randn=False, override_distorted=False):
        """
        use one of the available cell generation tools to sample cell parameters, to be fed to the discriminator
        """
        n_generators, generator_ind = self.what_generators_to_use(override_randn, override_distorted, override_adversarial)

        if (self.config.discriminator.train_adversarially or override_adversarial) and (generator_ind == 1):
            negative_type = 'generator'
            with torch.no_grad():
                generated_samples, _, _, generator_data = self.get_generator_samples(real_data)

                self.logger.update_stats_dict(self.epoch_type, 'generator_sample_source', np.zeros(len(generated_samples)), mode='extend')

        elif (self.config.discriminator.train_on_randn or override_randn) and (generator_ind == 2):
            generator_data = real_data  # no change to original
            negative_type = 'randn'

            generated_samples = self.gaussian_generator.forward(real_data.num_graphs, real_data).to(self.config.device)

            self.logger.update_stats_dict(self.epoch_type, 'generator_sample_source', np.ones(len(generated_samples)), mode='extend')

        elif (self.config.discriminator.train_on_distorted or override_distorted) and (generator_ind == 3):
            generator_data = real_data  # no change to original
            negative_type = 'distorted'

            generated_samples, distortion = self.make_distorted_samples(real_data)

            self.logger.update_stats_dict(self.epoch_type, 'generator_sample_source', 2 * np.ones(len(generated_samples)), mode='extend')
            self.logger.update_stats_dict(self.epoch_type, 'distortion level',
                                          torch.linalg.norm(distortion, axis=-1).cpu().detach().numpy(),
                                          mode='extend')
        else:
            print("No Generators set to make discriminator negatives!")
            assert False

        return generated_samples.float().detach(), negative_type, generator_data

    def make_distorted_samples(self, real_data, distortion_override=None):
        """
        given some cell params
        standardize them
        add noise in the standarized basis
        destandardize
        make sure samples are appropriately cleaned
        """
        generated_samples_std = (real_data.cell_params - self.lattice_means) / self.lattice_stds

        if distortion_override is not None:
            distortion = torch.randn_like(generated_samples_std) * distortion_override
        else:
            if self.config.discriminator.distortion_magnitude == -1:
                distortion = torch.randn_like(generated_samples_std) * torch.logspace(-.5, 0.5, len(generated_samples_std)).to(generated_samples_std.device)[:, None]  # wider range
            else:
                distortion = torch.randn_like(generated_samples_std) * self.config.discriminator.distortion_magnitude

        distorted_samples_std = (generated_samples_std + distortion).to(self.config.device)  # add jitter and return in standardized basis

        distorted_samples_clean = clean_cell_params(
            distorted_samples_std, real_data.sg_ind,
            self.lattice_means, self.lattice_stds,
            self.sym_info, self.supercell_builder.asym_unit_dict,
            rescale_asymmetric_unit=False, destandardize=True, mode='hard')

        return distorted_samples_clean, distortion

    def nov_22_figures(self):
        """
        make beautiful figures for the first paper
        """
        import plotly.io as pio
        pio.renderers.default = 'browser'

        # figures from the late 2022 JCTC draft submissions
        with wandb.init(config=self.config, project=self.config.wandb.project_name,
                        entity=self.config.wandb.username, tags=[self.config.logger.experiment_tag]):
            wandb.run.name = wandb.config.machine + '_' + str(
                wandb.config.run_num)  # overwrite procedurally generated run name with our run name
            wandb.run.save()

            # self.nice_dataset_analysis(self.prepped_dataset)
            self.misc_pre_training_items()
            from reporting.nov_22_regressor import nov_22_paper_regression_plots
            nov_22_paper_regression_plots(self.config)
            from reporting.nov_22_discriminator_final import nov_22_paper_discriminator_plots
            nov_22_paper_discriminator_plots(self.config, wandb)

        return

    def slash_batch(self, train_loader, test_loader, slash_fraction):
        slash_increment = max(4, int(train_loader.batch_size * slash_fraction))
        train_loader = update_dataloader_batch_size(train_loader, train_loader.batch_size - slash_increment)
        test_loader = update_dataloader_batch_size(test_loader, test_loader.batch_size - slash_increment)
        print('==============================')
        print('OOMOOMOOMOOMOOMOOMOOMOOMOOMOOM')
        print(f'Batch size slashed to {train_loader.batch_size} due to OOM')
        print('==============================')
        wandb.log({'batch size': train_loader.batch_size})

        return train_loader, test_loader

    def increment_batch_size(self, train_loader, test_loader, extra_test_loader):
        if self.config.grow_batch_size:
            if (train_loader.batch_size < len(train_loader.dataset)) and (
                    train_loader.batch_size < self.config.max_batch_size):  # if the batch is smaller than the dataset
                increment = max(4,
                                int(train_loader.batch_size * self.config.batch_growth_increment))  # increment batch size
                train_loader = update_dataloader_batch_size(train_loader, train_loader.batch_size + increment)
                test_loader = update_dataloader_batch_size(test_loader, test_loader.batch_size + increment)
                if extra_test_loader is not None:
                    extra_test_loader = update_dataloader_batch_size(extra_test_loader,
                                                                     extra_test_loader.batch_size + increment)
                print(f'Batch size incremented to {train_loader.batch_size}')
        wandb.log({'batch size': train_loader.batch_size})
        self.config.current_batch_size = train_loader.batch_size
        return train_loader, test_loader, extra_test_loader

    def model_checkpointing(self, epoch):
        if self.config.save_checkpoints:
            if epoch > 0:  # only save 'best' checkpoints
                if self.train_discriminator:
                    model = 'discriminator'
                    loss_record = self.logger.loss_record[model]['mean_test']
                    past_mean_losses = [np.mean(record) for record in loss_record]
                    if np.average(self.logger.current_losses[model]['mean_test']) < np.amin(past_mean_losses[:-1]):
                        print("Saving discriminator checkpoint")
                        save_checkpoint(epoch, self.discriminator, self.discriminator_optimizer, self.config.discriminator.__dict__,
                                        self.config.checkpoint_dir_path + 'best_discriminator_' + str(self.working_directory))
                if self.train_generator:
                    model = 'generator'
                    loss_record = self.logger.loss_record[model]['mean_test']
                    past_mean_losses = [np.mean(record) for record in loss_record]
                    if np.average(self.logger.current_losses[model]['mean_test']) < np.amin(past_mean_losses[:-1]):
                        print("Saving generator checkpoint")
                        save_checkpoint(epoch, self.generator, self.generator_optimizer, self.config.generator.__dict__,
                                        self.config.checkpoint_dir_path + 'best_generator_' + str(self.working_directory))
                if self.train_regressor:
                    model = 'regressor'
                    loss_record = self.logger.loss_record[model]['mean_test']
                    past_mean_losses = [np.mean(record) for record in loss_record]
                    if np.average(self.logger.current_losses[model]['mean_test']) < np.amin(past_mean_losses[:-1]):
                        print("Saving regressor checkpoint")
                        save_checkpoint(epoch, self.regressor, self.regressor_optimizer, self.config.regressor.__dict__,
                                        self.config.checkpoint_dir_path + 'best_regressor_' + str(self.working_directory))

        return None

    def update_lr(self):  # update learning rate
        self.discriminator_optimizer, discriminator_lr = set_lr(self.discriminator_schedulers, self.discriminator_optimizer,
                                                                self.config.discriminator.optimizer.lr_schedule,
                                                                self.config.discriminator.optimizer.min_lr,
                                                                self.config.discriminator.optimizer.max_lr,
                                                                self.logger.current_losses['discriminator']['mean_train'],
                                                                self.discriminator_hit_max_lr)

        self.generator_optimizer, generator_lr = set_lr(self.generator_schedulers, self.generator_optimizer,
                                                        self.config.generator.optimizer.lr_schedule,
                                                        self.config.generator.optimizer.min_lr,
                                                        self.config.generator.optimizer.max_lr,
                                                        self.logger.current_losses['generator']['mean_train'],
                                                        self.generator_hit_max_lr)

        self.regressor_optimizer, regressor_lr = set_lr(self.regressor_schedulers, self.regressor_optimizer,
                                                        self.config.regressor.optimizer.lr_schedule,
                                                        self.config.regressor.optimizer.min_lr,
                                                        self.config.regressor.optimizer.max_lr,
                                                        self.logger.current_losses['regressor']['mean_train'],
                                                        self.regressor_hit_max_lr)

        discriminator_learning_rate = self.discriminator_optimizer.param_groups[0]['lr']
        if discriminator_learning_rate >= self.config.discriminator.optimizer.max_lr:
            self.discriminator_hit_max_lr = True
        generator_learning_rate = self.generator_optimizer.param_groups[0]['lr']
        if generator_learning_rate >= self.config.generator.optimizer.max_lr:
            self.generator_hit_max_lr = True
        regressor_learning_rate = self.regressor_optimizer.param_groups[0]['lr']
        if regressor_learning_rate >= self.config.regressor.optimizer.max_lr:
            self.regressor_hit_max_lr = True

        (self.logger.learning_rates['discriminator'], self.logger.learning_rates['generator'],
         self.logger.learning_rates['regressor']) = (
            discriminator_learning_rate, generator_learning_rate, regressor_learning_rate)

    def reload_best_test_checkpoint(self, epoch):
        # reload best test
        if epoch != 0:  # if we have trained at all, reload the best model
            generator_path = f'../models/generator_{self.config.run_num}'
            discriminator_path = f'../models/discriminator_{self.config.run_num}'
            if os.path.exists(generator_path):
                generator_checkpoint = torch.load(generator_path)
                if list(generator_checkpoint['model_state_dict'])[0][0:6] == 'module':  # when we use dataparallel it breaks the state_dict - fix it by removing word 'module' from in front of everything
                    for i in list(generator_checkpoint['model_state_dict']):
                        generator_checkpoint['model_state_dict'][i[7:]] = generator_checkpoint['model_state_dict'].pop(i)
                self.generator.load_state_dict(generator_checkpoint['model_state_dict'])

            if os.path.exists(discriminator_path):
                discriminator_checkpoint = torch.load(discriminator_path)
                if list(discriminator_checkpoint['model_state_dict'])[0][0:6] == 'module':  # when we use dataparallel it breaks the state_dict - fix it by removing word 'module' from in front of everything
                    for i in list(discriminator_checkpoint['model_state_dict']):
                        discriminator_checkpoint['model_state_dict'][i[7:]] = discriminator_checkpoint['model_state_dict'].pop(i)
                self.discriminator.load_state_dict(discriminator_checkpoint['model_state_dict'])

    def gan_evaluation(self, epoch, test_loader, extra_test_loader):
        """
        run post-training evaluation
        """
        self.reload_best_test_checkpoint(epoch)

        # rerun test inference
        self.generator.eval()
        self.discriminator.eval()
        with torch.no_grad():
            self.run_epoch(epoch_type='test', data_loader=test_loader, update_gradients=False, record_stats=True, epoch=epoch)  # compute loss on test set

            # sometimes test the generator on a mini CSP problem
            if (self.config.mode == 'gan') and self.train_generator:
                pass # self.batch_csp(extra_test_loader if extra_test_loader is not None else test_loader)

            if extra_test_loader is not None:
                self.run_epoch(epoch_type='extra', data_loader=extra_test_loader, update_gradients=False, record_stats=True, epoch=epoch)  # compute loss on test set

        self.logger.log_epoch_analysis(test_loader)

    def compute_similarity_penalty(self, generated_samples, prior):
        """
        punish batches in which the samples are too self-similar

        Parameters
        ----------
        generated_samples
        prior

        Returns
        -------
        """
        if len(generated_samples) >= 3:
            # enforce that the distance between samples is similar to the distance between priors
            prior_dists = torch.cdist(prior, prior, p=2)
            std_samples = (generated_samples - self.lattice_means) / self.lattice_stds
            sample_dists = torch.cdist(std_samples, std_samples, p=2)
            prior_distance_penalty = F.smooth_l1_loss(input=sample_dists, target=prior_dists, reduction='none').mean(1)  # align distances to all other samples

            prior_variance = prior.var(dim=0)
            sample_variance = std_samples.var(dim=0)
            variance_penalty = F.smooth_l1_loss(input=sample_variance, target=prior_variance, reduction='none').mean().tile(len(prior))

            similarity_penalty = (prior_distance_penalty + variance_penalty)
            # TODO improve the distance between the different cell params are not at all equally meaningful
            # also in some SGs, lattice parameters are fixed, giving the model an escape from the similarity penalty

        else:
            similarity_penalty = None

        return similarity_penalty

    def score_adversarially(self, supercell_data, discriminator_noise=None, return_latent=False):
        """
        get an adversarial score for generated samples

        Parameters
        ----------
        supercell_data
        discriminator_noise
        return_latent

        Returns
        -------

        """
        if supercell_data is not None:  # if we built the supercells, we'll want to do this analysis anyway  # todo confirm we still want this functionality
            if discriminator_noise is not None:
                supercell_data.pos += torch.randn_like(
                    supercell_data.pos) * discriminator_noise
            else:
                if self.config.discriminator_positional_noise > 0:
                    supercell_data.pos += torch.randn_like(
                        supercell_data.pos) * self.config.discriminator_positional_noise

            if (self.config.device.lower() == 'cuda') and (supercell_data.x.device != 'cuda'):
                supercell_data = supercell_data.cuda()

            if self.config.test_mode or self.config.anomaly_detection:
                assert torch.sum(torch.isnan(supercell_data.x)) == 0, "NaN in training input"

            discriminator_score, dist_dict, latent = self.adversarial_score(supercell_data, return_latent=True)
        else:
            discriminator_score = None
            dist_dict = None
            latent = None

        if return_latent:
            return discriminator_score, dist_dict, latent
        else:
            return discriminator_score, dist_dict

    def aggregate_generator_losses(self, packing_loss, discriminator_raw_output, vdw_loss, vdw_score,
                                   similarity_penalty, packing_prediction, packing_target, h_bond_score):
        generator_losses_list = []
        stats_keys, stats_values = [], []
        if packing_loss is not None:
            packing_mae = np.abs(packing_prediction - packing_target) / packing_target

            if packing_mae.mean() < 0.025:  # dynamically soften the packing loss when the model is doing well
                self.packing_loss_coefficient *= 0.99
            if (packing_mae.mean() > 0.025) and (self.packing_loss_coefficient < 100):
                self.packing_loss_coefficient *= 1.01

            self.logger.packing_loss_coefficient = self.packing_loss_coefficient

            stats_keys += ['generator packing loss', 'generator_packing_prediction',
                           'generator_packing_target', 'generator packing mae']
            stats_values += [packing_loss.cpu().detach().numpy() * self.packing_loss_coefficient, packing_prediction,
                             packing_target, packing_mae]

            if True:  # enforce the target density all the time
                generator_losses_list.append(packing_loss.float() * self.packing_loss_coefficient)

        if discriminator_raw_output is not None:
            if self.config.generator.adversarial_loss_func == 'hot softmax':
                adversarial_loss = 1 - F.softmax(discriminator_raw_output / 5, dim=1)[:, 1]  # high temp smears out the function over a wider range
            elif self.config.generator.adversarial_loss_func == 'minimax':
                softmax_adversarial_score = F.softmax(discriminator_raw_output, dim=1)[:, 1]  # modified minimax
                adversarial_loss = -torch.log(softmax_adversarial_score)  # modified minimax
            elif self.config.generator.adversarial_loss_func == 'score':
                adversarial_loss = -softmax_and_score(discriminator_raw_output)  # linearized score
            elif self.config.generator.adversarial_loss_func == 'softmax':
                adversarial_loss = 1 - F.softmax(discriminator_raw_output, dim=1)[:, 1]
            else:
                print(f'{self.config.generator.adversarial_loss_func} is not an implemented adversarial loss')
                sys.exit()

            stats_keys += ['generator adversarial loss']
            stats_values += [adversarial_loss.cpu().detach().numpy()]
            stats_keys += ['generator adversarial score']
            stats_values += [discriminator_raw_output.cpu().detach().numpy()]

            if self.config.generator.train_adversarially:
                generator_losses_list.append(adversarial_loss)

        if vdw_loss is not None:
            stats_keys += ['generator per mol vdw loss', 'generator per mol vdw score']
            stats_values += [vdw_loss.cpu().detach().numpy()]
            stats_values += [vdw_score.cpu().detach().numpy()]

            if self.config.generator.train_vdw:
                generator_losses_list.append(vdw_loss)

        if h_bond_score is not None:
            if self.config.generator.train_h_bond:
                generator_losses_list.append(h_bond_score)

            stats_keys += ['generator h bond loss']
            stats_values += [h_bond_score.cpu().detach().numpy()]

        if similarity_penalty is not None:
            stats_keys += ['generator similarity loss']
            stats_values += [similarity_penalty.cpu().detach().numpy()]

            if self.config.generator.similarity_penalty != 0:
                if similarity_penalty is not None:
                    generator_losses_list.append(self.config.generator.similarity_penalty * similarity_penalty)
                else:
                    print('similarity penalty was none')

        generator_losses = torch.sum(torch.stack(generator_losses_list), dim=0)
        self.logger.update_stats_dict(self.epoch_type, stats_keys, stats_values, mode='extend')

        return generator_losses

    def reinitialize_models(self, generator, discriminator, regressor):
        """
        reset model weights, if we did not load it from a given path
        @param generator:
        @param discriminator:
        @param regressor:
        @return:
        """
        torch.manual_seed(self.config.seeds.model)
        print('Reinitializing models and optimizer')
        if self.config.generator_path is None:
            generator.apply(weight_reset)
        if self.config.discriminator_path is None:
            discriminator.apply(weight_reset)
        if self.config.regressor_path is None:
            regressor.apply(weight_reset)

        return generator, discriminator, regressor

    def reload_model_checkpoints(self, config):
        if config.generator_path is not None:
            generator_checkpoint = torch.load(config.generator_path)
            config.generator = Namespace(**generator_checkpoint['config'])  # overwrite the settings for the model

        if config.discriminator_path is not None:
            discriminator_checkpoint = torch.load(config.discriminator_path)
            config.discriminator = Namespace(**discriminator_checkpoint['config'])

        if config.regressor_path is not None:
            regressor_checkpoint = torch.load(config.regressor_path)
            config.regressor = Namespace(**regressor_checkpoint['config'])  # overwrite the settings for the model

        return config

    def batch_csp(self, data_loader):
        print('Starting Mini CSP')
        self.generator.eval()
        self.discriminator.eval()
        rdf_bins, rdf_range = 100, [0, 10]
        # todo move reporting into logger

        if self.config.target_identifiers is not None:  # analyse one or more particular crystals
            identifiers = [data_loader.dataset[ind].csd_identifier for ind in range(len(data_loader.dataset))]
            for i, identifier in enumerate(identifiers):
                '''prep data'''
                collater = Collater(None, None)
                real_data = collater([data_loader.dataset[i]]).to(self.config.device)
                real_data_for_sampling = collater([data_loader.dataset[i] for _ in range(data_loader.batch_size)]).to(self.config.device)  #
                real_samples_dict, real_supercell_data = self.analyze_real_crystals(real_data, rdf_bins, rdf_range)
                num_crystals, num_samples = 1, self.config.sample_steps

                '''do sampling'''
                generated_samples_dict, rr = self.generate_mini_csp_samples(real_data_for_sampling, rdf_range, rdf_bins)
                # results from batch to single array format
                for key in generated_samples_dict.keys():
                    if not isinstance(generated_samples_dict[key], list):
                        generated_samples_dict[key] = np.concatenate(generated_samples_dict[key], axis=0)[None, ...]
                    elif isinstance(generated_samples_dict[key], list):
                        generated_samples_dict[key] = [[generated_samples_dict[key][i2][i1] for i1 in range(num_samples) for i2 in range(real_data_for_sampling.num_graphs)]]

                '''results summary'''
                log_mini_csp_scores_distributions(self.config, wandb, generated_samples_dict, real_samples_dict, real_data, self.sym_info)
                log_csp_summary_stats(wandb, generated_samples_dict, self.sym_info)
                log_csp_cell_params(self.config, wandb, generated_samples_dict, real_samples_dict, identifier, crystal_ind=0)

                '''compute intra-crystal and crystal-target distances'''
                real_dists_dict, intra_dists_dict = compute_csp_sample_distances(self.config, real_samples_dict, generated_samples_dict, num_crystals, num_samples * real_data_for_sampling.num_graphs, rr)

                plot_mini_csp_dist_vs_score(real_dists_dict['real_sample_rdf_distance'],
                                            real_dists_dict['real_sample_cell_distance'],
                                            real_dists_dict['real_sample_latent_distance'],
                                            generated_samples_dict, real_samples_dict, wandb)

                sample_density_funnel_plot(self.config, wandb, num_crystals, identifier, generated_samples_dict, real_samples_dict)
                sample_rdf_funnel_plot(self.config, wandb, num_crystals, identifier, generated_samples_dict['score'], real_samples_dict, real_dists_dict['real_sample_rdf_distance'])

                '''cluster and identify interesting samples, then optimize them'''
                aa = 1

        else:  # otherwise, a random batch from the dataset
            collater = Collater(None, None)
            real_data = collater(data_loader.dataset[0:min(50, len(data_loader.dataset))]).to(self.config.device)  # take a fixed number of samples
            real_samples_dict, real_supercell_data = self.analyze_real_crystals(real_data, rdf_bins, rdf_range)
            num_samples = self.config.sample_steps
            num_crystals = real_data.num_graphs

            '''do sampling'''
            generated_samples_dict, rr = self.generate_mini_csp_samples(real_data, rdf_range, rdf_bins)

            '''results summary'''
            log_mini_csp_scores_distributions(self.config, wandb, generated_samples_dict, real_samples_dict, real_data, self.sym_info)
            log_csp_summary_stats(wandb, generated_samples_dict, self.sym_info)
            for ii in range(real_data.num_graphs):
                log_csp_cell_params(self.config, wandb, generated_samples_dict, real_samples_dict, real_data.csd_identifier[ii], crystal_ind=ii)

            '''compute intra-crystal and crystal-target distances'''
            real_dists_dict, intra_dists_dict = compute_csp_sample_distances(self.config, real_samples_dict, generated_samples_dict, num_crystals, num_samples, rr)

            '''summary distances'''
            plot_mini_csp_dist_vs_score(real_dists_dict['real_sample_rdf_distance'],
                                        real_dists_dict['real_sample_cell_distance'],
                                        real_dists_dict['real_sample_latent_distance'],
                                        generated_samples_dict, real_samples_dict, wandb)

            '''funnel plots'''
            sample_density_funnel_plot(self.config, wandb, num_crystals, real_data.csd_identifier, generated_samples_dict, real_samples_dict)
            sample_rdf_funnel_plot(self.config, wandb, num_crystals, real_data.csd_identifier, generated_samples_dict['score'], real_samples_dict, real_dists_dict['real_sample_rdf_distance'])

        return None

    def analyze_real_crystals(self, real_data, rdf_bins, rdf_range):
        real_supercell_data = self.supercell_builder.prebuilt_unit_cell_to_supercell(real_data, self.config.supercell_size, self.config.discriminator.model.convolution_cutoff)

        discriminator_score, dist_dict, discriminator_latent = self.score_adversarially(real_supercell_data.clone(), self.discriminator, return_latent=True)
        h_bond_score = self.compute_h_bond_score(real_supercell_data)
        _, vdw_score, _, _ = vdw_overlap(self.vdw_radii,
                                         dist_dict=dist_dict,
                                         num_graphs=real_data.num_graphs,
                                         graph_sizes=real_data.mol_size)

        real_rdf, rr, atom_inds = crystal_rdf(real_supercell_data, rrange=rdf_range,
                                              bins=rdf_bins, mode='intermolecular',
                                              raw_density=True, atomwise=True, cpu_detach=True)

        volumes_list = []
        for i in range(real_data.num_graphs):
            volumes_list.append(cell_vol_torch(real_data.cell_params[i, 0:3], real_data.cell_params[i, 3:6]))
        volumes = torch.stack(volumes_list)
        real_packing_coeffs = real_data.mult * real_data.mol_volume / volumes

        real_samples_dict = {'score': softmax_and_score(discriminator_score).cpu().detach().numpy(),
                             'vdw overlap': -vdw_score.cpu().detach().numpy(),
                             'density': real_packing_coeffs.cpu().detach().numpy(),
                             'h bond score': h_bond_score.cpu().detach().numpy(),
                             'cell params': real_data.cell_params.cpu().detach().numpy(),
                             'space group': real_data.sg_ind.cpu().detach().numpy(),
                             'RDF': real_rdf,
                             'discriminator latent': discriminator_latent,
                             }

        return real_samples_dict, real_supercell_data.cpu()

    def generate_mini_csp_samples(self, real_data, rdf_range, rdf_bins, sample_source='generator'):
        num_molecules = real_data.num_graphs
        n_sampling_iters = self.config.sample_steps
        sampling_dict = {'score': np.zeros((num_molecules, n_sampling_iters)),
                         'vdw overlap': np.zeros((num_molecules, n_sampling_iters)),
                         'density': np.zeros((num_molecules, n_sampling_iters)),
                         'h bond score': np.zeros((num_molecules, n_sampling_iters)),
                         'cell params': np.zeros((num_molecules, n_sampling_iters, 12)),
                         'space group': np.zeros((num_molecules, n_sampling_iters)),
                         'handedness': np.zeros((num_molecules, n_sampling_iters)),
                         'distortion_size': np.zeros((num_molecules, n_sampling_iters)),
                         'discriminator latent': np.zeros((num_molecules, n_sampling_iters, self.config.discriminator.fc_depth)),
                         'RDF': [[] for _ in range(num_molecules)]
                         }

        with torch.no_grad():
            for ii in tqdm.tqdm(range(n_sampling_iters)):
                fake_data = real_data.clone().to(self.config.device)

                if sample_source == 'generator':
                    # use generator to make samples
                    samples, prior, standardized_target_packing_coeff, fake_data = \
                        self.get_generator_samples(fake_data)

                    fake_supercell_data, generated_cell_volumes, _ = \
                        self.supercell_builder.build_supercells(
                            fake_data, samples, self.config.supercell_size,
                            self.config.discriminator.model.convolution_cutoff,
                            align_molecules=False,
                        )

                elif sample_source == 'distorted':
                    # test - do slight distortions on existing crystals
                    generated_samples_ii = (real_data.cell_params - self.lattice_means) / self.lattice_stds

                    if True:  # self.config.discriminator.distortion_magnitude == -1:
                        distortion = torch.randn_like(generated_samples_ii) * torch.logspace(-4, 1, len(generated_samples_ii)).to(generated_samples_ii.device)[:, None]  # wider range
                        distortion = distortion[torch.randperm(len(distortion))]
                    else:
                        distortion = torch.randn_like(generated_samples_ii) * self.config.discriminator.distortion_magnitude

                    generated_samples_i_d = (generated_samples_ii + distortion).to(self.config.device)  # add jitter and return in standardized basis

                    generated_samples_i = clean_cell_params(
                        generated_samples_i_d, real_data.sg_ind,
                        self.lattice_means, self.lattice_stds,
                        self.sym_info, self.supercell_builder.asym_unit_dict,
                        rescale_asymmetric_unit=False, destandardize=True, mode='hard')

                    fake_supercell_data, generated_cell_volumes, _ = self.supercell_builder.build_supercells(
                        fake_data, generated_samples_i, self.config.supercell_size,
                        self.config.discriminator.model.convolution_cutoff,
                        align_molecules=True,
                        target_handedness=real_data.asym_unit_handedness,
                    )
                    sampling_dict['distortion_size'][:, ii] = torch.linalg.norm(distortion, axis=-1).cpu().detach().numpy()
                    # end test

                generated_rdf, rr, atom_inds = crystal_rdf(fake_supercell_data, rrange=rdf_range,
                                                           bins=rdf_bins, mode='intermolecular',
                                                           raw_density=True, atomwise=True, cpu_detach=True)
                discriminator_score, dist_dict, discriminator_latent = self.score_adversarially(fake_supercell_data.clone(), discriminator_noise=0, return_latent=True)
                h_bond_score = self.compute_h_bond_score(fake_supercell_data)
                vdw_score = vdw_overlap(self.vdw_radii,
                                        dist_dict=dist_dict,
                                        num_graphs=fake_data.num_graphs,
                                        graph_sizes=fake_data.mol_size,
                                        return_score_only=True)

                volumes_list = []
                for i in range(fake_data.num_graphs):
                    volumes_list.append(
                        cell_vol_torch(fake_supercell_data.cell_params[i, 0:3], fake_supercell_data.cell_params[i, 3:6]))
                volumes = torch.stack(volumes_list)

                fake_packing_coeffs = fake_supercell_data.mult * fake_supercell_data.mol_volume / volumes

                sampling_dict['score'][:, ii] = softmax_and_score(discriminator_score).cpu().detach().numpy()
                sampling_dict['vdw overlap'][:, ii] = -vdw_score.cpu().detach().numpy()
                sampling_dict['density'][:, ii] = fake_packing_coeffs.cpu().detach().numpy()
                sampling_dict['h bond score'][:, ii] = h_bond_score.cpu().detach().numpy()
                sampling_dict['cell params'][:, ii, :] = fake_supercell_data.cell_params.cpu().detach().numpy()
                sampling_dict['space group'][:, ii] = fake_supercell_data.sg_ind.cpu().detach().numpy()
                sampling_dict['handedness'][:, ii] = fake_supercell_data.asym_unit_handedness.cpu().detach().numpy()
                sampling_dict['discriminator latent'][:, ii, :] = discriminator_latent
                for jj in range(num_molecules):
                    sampling_dict['RDF'][jj].append(generated_rdf[jj])

        return sampling_dict, rr

    def compute_h_bond_score(self, supercell_data=None):
        if (supercell_data is not None) and ('atom_is_H_bond_donor' in self.dataDims['atom_features']) and (
                'molecule_num_donors' in self.dataDims['molecule_features']):  # supercell_data is not None: # do vdw computation even if we don't need it
            # get the total per-molecule counts
            mol_acceptors = supercell_data.tracking[:, self.dataDims['tracking_features'].index('molecule_num_acceptors')]
            mol_donors = supercell_data.tracking[:, self.dataDims['tracking_features'].index('molecule_num_donors')]

            '''
            count pairs within a close enough bubble ~2.7-3.3 Angstroms
            '''
            h_bonds_loss = []
            for i in range(supercell_data.num_graphs):
                if (mol_donors[i]) > 0 and (mol_acceptors[i] > 0):
                    h_bonds = compute_num_h_bonds(supercell_data,
                                                  self.dataDims['atom_features'].index('atom_is_H_bond_acceptor'),
                                                  self.dataDims['atom_features'].index('atom_is_H_bond_donor'), i)

                    bonds_per_possible_bond = h_bonds / min(mol_donors[i], mol_acceptors[i])
                    h_bond_loss = 1 - torch.tanh(2 * bonds_per_possible_bond)  # smoother gradient about 0

                    h_bonds_loss.append(h_bond_loss)
                else:
                    h_bonds_loss.append(torch.zeros(1)[0].to(supercell_data.x.device))
            h_bond_loss_f = torch.stack(h_bonds_loss)
        else:
            h_bond_loss_f = None

        return h_bond_loss_f

    def generator_density_matching_loss(self, standardized_target_packing,
                                        data, raw_sample,
                                        precomputed_volumes=None, loss_func='mse'):
        """
        compute packing coefficients for generated cells
        compute losses relating to packing density
        """
        if precomputed_volumes is None:
            volumes_list = []
            for i in range(len(raw_sample)):
                volumes_list.append(cell_vol_torch(data.cell_params[i, 0:3], data.cell_params[i, 3:6]))
            volumes = torch.stack(volumes_list)
        else:
            volumes = precomputed_volumes

        generated_packing_coeffs = data.mult * data.mol_volume / volumes
        standardized_gen_packing_coeffs = (generated_packing_coeffs - self.std_dict['crystal_packing_coefficient'][0]) / self.std_dict['crystal_packing_coefficient'][1]

        target_packing_coeffs = standardized_target_packing * self.std_dict['crystal_packing_coefficient'][1] + self.std_dict['crystal_packing_coefficient'][0]

        csd_packing_coeffs = data.tracking[:, self.dataDims['tracking_features'].index('crystal_packing_coefficient')]

        # compute loss vs the target
        if loss_func == 'mse':
            packing_loss = F.mse_loss(standardized_gen_packing_coeffs, standardized_target_packing,
                                      reduction='none')  # allow for more error around the minimum
        elif loss_func == 'l1':
            packing_loss = F.smooth_l1_loss(standardized_gen_packing_coeffs, standardized_target_packing,
                                            reduction='none')
        else:
            assert False, "Must pick from the set of implemented packing loss functions 'mse', 'l1'"
        return packing_loss, generated_packing_coeffs, target_packing_coeffs, csd_packing_coeffs
