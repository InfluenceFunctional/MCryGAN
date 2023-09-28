import sys

import numpy as np
import torch
from torch import optim, nn as nn
from torch.nn import functional as F
from torch.optim import lr_scheduler as lr_scheduler

from common.geometry_calculations import cell_vol_torch
from common.utils import np_softmax, components2angle


def get_grad_norm(model):
    params = [p for p in model.parameters() if p.grad is not None and p.requires_grad]
    if len(params) == 0:
        norm = 0
    else:
        norm = torch.norm(torch.stack([torch.norm(p.grad.detach()).cpu() for p in params]), 2.0).item()

    return norm


def set_lr(schedulers, optimizer, lr_schedule, min_lr, max_lr, err_tr, hit_max_lr):
    if lr_schedule:
        lr = optimizer.param_groups[0]['lr']
        if lr > min_lr:
            schedulers[0].step(np.mean(np.asarray(err_tr)))  # plateau scheduler

        if not hit_max_lr:
            schedulers[1].step()
        elif hit_max_lr:
            if lr > min_lr:
                schedulers[2].step()  # start reducing lr

    lr = optimizer.param_groups[0]['lr']
    return optimizer, lr


def compute_F1_score(confusion_matrix, num_classes):
    true_positive = [confusion_matrix[i, i] for i in range(num_classes)]
    false_positive = [np.sum(confusion_matrix[i, :]) - confusion_matrix[i, i] for i in range(num_classes)]
    false_negative = [np.sum(confusion_matrix[:, i]) - confusion_matrix[i, i] for i in range(num_classes)]

    accuracy = np.sum(confusion_matrix.diagonal()) / np.sum(confusion_matrix)
    recall = np.asarray([true_positive[i] / (true_positive[i] + false_positive[i]) for i in range(num_classes)])
    precision = np.asarray([true_positive[i] / (true_positive[i] + false_negative[i]) for i in range(num_classes)])
    F1 = np.asarray([2 * precision[i] * recall[i] / (precision[i] + recall[i]) for i in range(num_classes)])

    return accuracy, np.average(np.nan_to_num(precision, nan=0)), np.average(np.nan_to_num(recall, nan=0)), np.average(np.nan_to_num(F1, nan=0))


def compute_top_k_accuracy(config, probs, targets, X=5):
    # this actually computes the 'true positive rate' true positive / all positives
    correct_counter = np.zeros(config.dataDims['output classes'][0], dtype='uint64')
    incorrect_counter = np.zeros_like(correct_counter)

    for i in range(len(probs)):
        topXPredictions = np.argpartition(probs[i], -X)[-X:]  # not sorted
        if targets[i] in topXPredictions:
            correct_counter[targets[i]] += 1
        else:
            incorrect_counter[targets[i]] += 1

    overall_top_x_accuracy = correct_counter.sum() / (correct_counter.sum() + incorrect_counter.sum())
    by_group_top_x_accuracy = np.zeros(len(correct_counter))
    for i in range(len(correct_counter)):
        by_group_top_x_accuracy[i] = correct_counter[i] / (correct_counter[i] + incorrect_counter[i])

    return overall_top_x_accuracy, by_group_top_x_accuracy


def check_convergence(record, history, convergence_eps):
    """
    check if we are converged
    condition: test loss has increased or levelled out over the last several epochs
    :return: convergence flag
    """

    converged = False
    if type(record) == list:
        record = np.concatenate(record)
    elif isinstance(record, np.ndarray):
        record = record.copy()

    if len(record) > (history + 2):
        if all(record[-history:] >= np.amin(record)):
            converged = True
            print("Model converged, target diverging")

        criteria = np.var(record[-history:]) / np.abs(np.average(record[-history:]))
        print('Convergence criteria at {:.3f}'.format(np.log10(criteria)))  # todo better rolling metric here - trailing exponential something
        if criteria < convergence_eps:
            converged = True
            print("Model converged, target stabilized")

    return converged


def save_model(model, optimizer):
    torch.save({'model_state_dict': model.state_dict(), 'optimizer_state_dict': optimizer.state_dict()}, 'ckpts/model_ckpt')


def init_optimizer(optim_config, model, freeze_params=False):
    """
    initialize optimizers
    @param optim_config: config for a given optimizer
    @param model: model with params to be optimized
    @param freeze_params: whether parameters without requires_grad should be frozen
    @return: optimizer
    """

    amsgrad = True
    beta1 = optim_config.beta1  # 0.9
    beta2 = optim_config.beta2  # 0.999
    weight_decay = optim_config.weight_decay  # 0.01
    momentum = 0

    if freeze_params:
        model_params = [param for param in model.parameters() if param.requires_grad == True]
    else:
        model_params = model.parameters()

    if optim_config.optimizer == 'adam':
        optimizer = optim.Adam(model_params, amsgrad=amsgrad, lr=optim_config.init_lr, betas=(beta1, beta2), weight_decay=weight_decay)
    elif optim_config.optimizer == 'adamw':
        optimizer = optim.AdamW(model_params, amsgrad=amsgrad, lr=optim_config.init_lr, betas=(beta1, beta2), weight_decay=weight_decay)
    elif optim_config.optimizer == 'sgd':
        optimizer = optim.SGD(model_params, lr=optim_config.init_lr, momentum=momentum, weight_decay=weight_decay)
    else:
        print(optim_config.optimizer + ' is not a valid optimizer')
        sys.exit()

    return optimizer


def init_schedulers(config, optimizer):
    """
    initialize a series of LR schedulers
    @param config: config for the given optimizer
    @param optimizer:
    @return: set of schedulers
    """
    scheduler1 = lr_scheduler.ReduceLROnPlateau(
        optimizer,
        mode='min',
        factor=0.5,
        patience=500,
        threshold=1e-4,
        threshold_mode='rel',
        cooldown=500
    )
    lr_lambda = lambda epoch: config.lr_growth_lambda
    scheduler2 = lr_scheduler.MultiplicativeLR(optimizer, lr_lambda=lr_lambda)
    lr_lambda2 = lambda epoch: config.lr_shrink_lambda
    scheduler3 = lr_scheduler.MultiplicativeLR(optimizer, lr_lambda=lr_lambda2)

    return [scheduler1, scheduler2, scheduler3]


def softmax_and_score(raw_classwise_output, temperature=1, old_method=False, correct_discontinuity=True):
    """
    Parameters
    ----------
    raw_classwise_output: numpy array or torch tensor with dimension [n,2], representing the non-normalized [false,true] probabilities
    temperature: softmax temperature
    old_method: use more complicated method from first paper
    correct_discontinuity: correct discontinuity at 0 only in the old method


    Returns
    -------
    score: linearizes the input probabilities from (0,1) to [-inf, inf] for easier visualization
    """
    if not old_method:  # turns out you get almost identically the same answer by simply dividing the activations, much simpler
        if torch.is_tensor(raw_classwise_output):
            soft_activation = F.softmax(raw_classwise_output, dim=-1)
            score = torch.log10(soft_activation[:, 1] / soft_activation[:, 0])
            assert torch.sum(torch.isnan(score)) == 0
            return score
        else:
            soft_activation = np_softmax(raw_classwise_output)
            score = np.log10(soft_activation[:, 1] / soft_activation[:, 0])
            assert np.sum(np.isnan(score)) == 0
            return score
    else:
        if correct_discontinuity:
            correction = 1
        else:
            correction = 0

        if isinstance(raw_classwise_output, np.ndarray):
            softmax_output = np_softmax(raw_classwise_output.astype('float64'), temperature)[:, 1].astype('float64')  # values get too close to zero for float32
            tanned = np.tan((softmax_output - 0.5) * np.pi)
            sign = (raw_classwise_output[:, 1] > raw_classwise_output[:, 0]) * 2 - 1  # values very close to zero can realize a sign error
            return sign * np.log10(correction + np.abs(tanned))  # new factor of 1+ conditions the function about zero

        elif torch.is_tensor(raw_classwise_output):
            softmax_output = F.softmax(raw_classwise_output / temperature, dim=-1)[:, 1]
            tanned = torch.tan((softmax_output - 0.5) * torch.pi)
            sign = (raw_classwise_output[:, 1] > raw_classwise_output[:, 0]) * 2 - 1  # values very close to zero can realize a sign error
            return sign * torch.log10(correction + torch.abs(tanned))


def norm_scores(score, tracking_features, dataDims):
    """
    norm the incoming score according to some feature of the molecule (generally size)
    """
    volume = tracking_features[:, dataDims['tracking_features'].index('molecule volume')]
    # radius = (3/4/np.pi * volume)**(1/3)
    # surface_area = 4*np.pi*radius**2
    # eccentricity = tracking_features[:,config.dataDims['tracking_features'].index('molecule eccentricity')]
    # surface_area = tracking_features[:,config.dataDims['tracking_features'].index('molecule freeSASA')]
    return score / volume


def enforce_1d_bound(x: torch.tensor, x_span, x_center, mode='soft'):  # soft or hard
    """
    constrains function to range x_center plus/minus x_span
    Parameters
    ----------
    x
    x_span
    x_center
    mode

    Returns
    -------

    """
    if mode == 'soft':  # smoothly converge to (center-span,center+span)
        bounded = F.tanh((x - x_center) / x_span) * x_span + x_center
    elif mode == 'hard':  # linear scaling to hard stop at [center-span, center+span]
        bounded = F.hardtanh((x - x_center) / x_span) * x_span + x_center
    else:
        raise ValueError("bound must be of type 'hard' or 'soft'")

    return bounded


def undo_1d_bound(x: torch.tensor, x_span, x_center, mode='soft'):
    """
    undo / rescale an enforced 1d bound
    only setup for soft rescaling
    """
    # todo: hard mode

    if mode == 'soft':
        return x_span * torch.atanh((x - x_center) / x_span) + x_center
    elif mode == 'hard':  # linear scaling to hard stop at [center-span, center+span]
        raise ValueError("'hard' bound not yet implemented")
    else:
        raise ValueError("bound must be of type 'soft'")


def reload_model(model, optimizer, path, reload_optimizer=False):
    """
    load model and state dict from path
    includes fix for potential dataparallel issue
    """
    checkpoint = torch.load(path)
    if list(checkpoint['model_state_dict'])[0][0:6] == 'module':  # when we use dataparallel it breaks the state_dict - fix it by removing word 'module' from in front of everything
        for i in list(checkpoint['model_state_dict']):
            checkpoint['model_state_dict'][i[7:]] = checkpoint['model_state_dict'].pop(i)

    model.load_state_dict(checkpoint['model_state_dict'])
    if optimizer is not None:
        if reload_optimizer:
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])

    return model, optimizer


def compute_packing_coefficient(cell_params: torch.tensor, mol_volumes: torch.tensor, z_values: torch.tensor):
    """
    @param cell_params: cell parameters using our standard scheme 0-5 are a,b,c,alpha,beta,gamma
    @param mol_volumes: molumes in cubic angstrom of each single molecule
    @param z_values: Z value for each crystal
    @return: crystal packing coefficient
    """
    volumes_list = []
    for i in range(len(cell_params)):
        volumes_list.append(cell_vol_torch(cell_params[i, 0:3], cell_params[i, 3:6]))
    cell_volumes = torch.stack(volumes_list)
    coeffs = z_values * mol_volumes / cell_volumes
    return coeffs


def compute_num_h_bonds(supercell_data, atom_acceptor_ind, atom_donor_ind, i):
    """
    compute the number of hydrogen bonds, up to a loose range (3.3 angstroms), and non-directionally
    @param atom_donor_ind: index in tracking features to find donor status
    @param atom_acceptor_ind: index in tracking features to find acceptor status
    @param supercell_data: crystal data
    @param dataDims: useful information
    @param i: cell index we are checking
    @return: sum of total hydrogen bonds for the canonical conformer
    """
    batch_inds = torch.arange(supercell_data.ptr[i], supercell_data.ptr[i + 1], device=supercell_data.x.device)

    # find the canonical conformers
    canonical_conformers_inds = torch.where(supercell_data.aux_ind[batch_inds] == 0)[0]
    outside_inds = torch.where(supercell_data.aux_ind[batch_inds] == 1)[0]

    # identify and count canonical conformer acceptors and intermolecular donors
    canonical_conformer_acceptors_inds = torch.where(supercell_data.x[batch_inds[canonical_conformers_inds], atom_acceptor_ind] == 1)[0]
    outside_donors_inds = torch.where(supercell_data.x[batch_inds[outside_inds], atom_donor_ind] == 1)[0]

    donors_pos = supercell_data.pos[batch_inds[outside_inds[outside_donors_inds]]]
    acceptors_pos = supercell_data.pos[batch_inds[canonical_conformers_inds[canonical_conformer_acceptors_inds]]]

    return torch.sum(torch.cdist(donors_pos, acceptors_pos, p=2) < 3.3)


def get_strides(n_target_bins, init_size=3):
    """
    compute the deconvolution stride size for an approximately fixed number of deconvolution steps
    required to achieve a certain output size
    @param n_target_bins: desired cubic edge length
    @param init_size: edge length of input
    @return: list of the desired strides
    """
    target_size = n_target_bins
    tolerance = -1
    converged = False
    while not converged and tolerance < 4:
        tolerance += 1
        for i in range(4):
            for j in range(4):
                for k in range(4):
                    for l in range(4):
                        inds = [1, 2, 3, 4]
                        strides = [inds[i], inds[j], inds[k], inds[l]]
                        img_size = [init_size]
                        for ii, stride in enumerate(strides):
                            if stride == 1:
                                img_size += [img_size[ii] + 2]
                            elif stride == 2:
                                img_size += [img_size[ii] + img_size[ii] + 1]
                            elif stride == 3:
                                img_size += [img_size[ii] + 2 * img_size[ii]]
                            elif stride == 4:
                                img_size += [img_size[ii] + 3 * img_size[ii] - 1]

                        if (img_size[-1] == target_size - tolerance):
                            converged = True
                        if converged:
                            # print(strides)
                            # print(img_size[-1])
                            break
                    if converged:
                        break
                if converged:
                    break
            if converged:
                break

    for n in range(tolerance):
        strides += [1]

    if converged:
        return strides, img_size[-1] + 2 * tolerance
    else:
        assert False, 'could not manage this resolution with current strided setup'


def save_checkpoint(epoch, model, optimizer, config, save_path):
    torch.save({'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'config': config},
               save_path)
    return None


def compute_h_bond_score(feature_richness, atom_acceptor_ind, atom_donor_ind, num_acceptors_ind, num_donors_ind, supercell_data=None):
    if (supercell_data is not None) and (
            feature_richness == 'full'):  # supercell_data is not None: # do vdw computation even if we don't need it
        # get the total per-molecule counts
        mol_acceptors = supercell_data.tracking[:, num_acceptors_ind]
        mol_donors = supercell_data.tracking[:, num_donors_ind]

        '''
        count pairs within a close enough bubble ~2.7-3.3 Angstroms
        '''
        h_bonds_loss = []
        for i in range(supercell_data.num_graphs):
            if (mol_donors[i]) > 0 and (mol_acceptors[i] > 0):
                h_bonds = compute_num_h_bonds(supercell_data, atom_acceptor_ind, atom_donor_ind, i)

                bonds_per_possible_bond = h_bonds / min(mol_donors[i], mol_acceptors[i])
                h_bond_loss = 1 - torch.tanh(2 * bonds_per_possible_bond)  # smoother gradient about 0

                h_bonds_loss.append(h_bond_loss)
            else:
                h_bonds_loss.append(torch.zeros(1)[0].to(supercell_data.x.device))
        h_bond_loss_f = torch.stack(h_bonds_loss)
    else:
        h_bond_loss_f = None

    return h_bond_loss_f

def cell_density_loss(packing_loss_rescaling, packing_coeff_ind, mol_volume_ind,
                      packing_mean, packing_std, data, raw_sample, precomputed_volumes=None):
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

    generated_packing_coeffs = data.mult * data.tracking[:, mol_volume_ind] / volumes
    standardized_gen_packing_coeffs = (generated_packing_coeffs - packing_mean) / packing_std

    csd_packing_coeffs = data.tracking[:, packing_coeff_ind]
    standardized_csd_packing_coeffs = (csd_packing_coeffs - packing_mean) / packing_std  # requires that packing coefficnet is set as regression target in main

    if packing_loss_rescaling == 'log':
        packing_loss = torch.log(
            1 + F.smooth_l1_loss(standardized_gen_packing_coeffs, standardized_csd_packing_coeffs,
                                 reduction='none'))  # log(1+loss) is a soft rescaling to avoid gigantic losses
    elif packing_loss_rescaling is None:
        packing_loss = F.smooth_l1_loss(standardized_gen_packing_coeffs, standardized_csd_packing_coeffs,
                                        reduction='none')
    elif packing_loss_rescaling == 'mse':
        packing_loss = F.mse_loss(standardized_gen_packing_coeffs, standardized_csd_packing_coeffs,
                                  reduction='none')

    assert torch.sum(torch.isnan(packing_loss)) == 0

    return packing_loss, generated_packing_coeffs, csd_packing_coeffs


def generator_density_matching_loss(standardized_target_packing, packing_mean, packing_std,
                                    mol_volume_ind, packing_coeff_ind,
                                    data, raw_sample,
                                    precomputed_volumes=None, loss_func='mse'):
    """ # todo deprecate mol_volume_ind
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
    standardized_gen_packing_coeffs = (generated_packing_coeffs - packing_mean) / packing_std

    target_packing_coeffs = standardized_target_packing * packing_std + packing_mean

    csd_packing_coeffs = data.tracking[:, packing_coeff_ind]

    # compute loss vs the target
    if loss_func == 'mse':
        packing_loss = F.mse_loss(standardized_gen_packing_coeffs, standardized_target_packing,
                                  reduction='none')  # allow for more error around the minimum
    elif loss_func == 'l1':
        packing_loss = F.smooth_l1_loss(standardized_gen_packing_coeffs, standardized_target_packing,
                                        reduction='none')

    return packing_loss, generated_packing_coeffs, target_packing_coeffs, csd_packing_coeffs


def compute_combo_score(packing_prediction, vdw_penalty, discriminator_raw_output):
    # combo
    f1 = 100  # for sharp sigmoid scaling
    # accept packing within range 0.675 +/- 0.125
    packing_center = 0.675
    packing_span = 0.125
    packing_range_loss = F.sigmoid(-f1 * (packing_prediction - packing_center + packing_span)) * (-(packing_prediction - packing_center)) + \
                         F.sigmoid(f1 * (packing_prediction - packing_center - packing_span)) * (packing_prediction - packing_center)

    vdw_span = 0.5  # accept vdw overlaps of up to 0.5 angstrom
    vdw_range_loss = F.sigmoid(f1 * (vdw_penalty - vdw_span)) * vdw_penalty

    # combo score is the two above bracketing losses plus the adversarial probability we want to minimize
    discriminator_loss = F.softmax(discriminator_raw_output / 5, dim=-1)[:, 0]  # high temperature for more linear gradient
    combo_score = -(vdw_range_loss + packing_range_loss + discriminator_loss)
    return combo_score


def weight_reset(m):
    if isinstance(m, nn.Conv2d) or isinstance(m, nn.Linear) or isinstance(m, nn.Conv3d) or isinstance(m, nn.ConvTranspose3d):
        m.reset_parameters()


def get_n_config(model):
    """
    count parameters for a pytorch model
    :param model:
    :return:
    """
    pp = 0
    for p in list(model.parameters()):
        nn = 1
        for s in list(p.size()):
            nn = nn * s
        pp += nn
    return pp


def clean_generator_output(samples, lattice_means, lattice_stds, destandardize=True, mode='soft'):
    """
    convert from raw model output to the actual cell parameters with appropriate bounds
    considering raw outputs to be in the standardized basis, we destandardize, then enforce bounds
    """

    '''separate components'''
    lattice_lengths = samples[:, :3]
    lattice_angles = samples[:, 3:6]
    mol_positions = samples[:, 6:9]
    mol_orientations = samples[:, 9:]

    '''destandardize & decode angles'''
    if destandardize:
        real_lattice_lengths = lattice_lengths * lattice_stds[:3] + lattice_means[:3]
        real_lattice_angles = lattice_angles * lattice_stds[3:6] + lattice_means[3:6]  # not bothering to encode as an angle
        real_mol_positions = mol_positions * lattice_stds[6:9] + lattice_means[6:9]
        if samples.shape[-1] == 12:
            real_mol_orientations = mol_orientations * lattice_stds[9:] + lattice_means[9:]
        else:
            real_mol_orientations = mol_orientations * 1
    else:  # optionally, skip destandardization if we are already in the real basis
        real_lattice_lengths = lattice_lengths * 1
        real_lattice_angles = lattice_angles * 1
        real_mol_positions = mol_positions * 1
        real_mol_orientations = mol_orientations * 1

    if samples.shape[-1] == 15:
        theta, phi, r_i = decode_to_sph_rotvec(real_mol_orientations)
    elif samples.shape[-1] == 12:  # already have angles, no need to decode
        theta = enforce_1d_bound(real_mol_orientations[:, 0], x_span=torch.pi / 4, x_center=torch.pi / 4, mode=mode)[:, None]
        phi = enforce_1d_bound(real_mol_orientations[:, 1], x_span=torch.pi, x_center=0, mode=mode)[:, None]
        r_i = enforce_1d_bound(real_mol_orientations[:, 2], x_span=torch.pi, x_center=torch.pi, mode=mode)[:, None]

    r = torch.maximum(r_i, torch.ones_like(r_i) * 0.01)  # MUST be nonzero
    clean_mol_orientations = torch.cat((theta, phi, r), dim=-1)

    '''enforce physical bounds'''
    if mode == 'soft':
        clean_lattice_lengths = F.softplus(real_lattice_lengths - 0.1) + 0.1  # smoothly enforces positive nonzero
    else:
        clean_lattice_lengths = torch.maximum(F.relu(real_lattice_lengths), torch.ones_like(real_lattice_lengths))  # harshly enforces positive nonzero

    clean_lattice_angles = enforce_1d_bound(real_lattice_angles, x_span=torch.pi / 2 * 0.8, x_center=torch.pi / 2, mode=mode)  # range from (0,pi) with 20% limit to prevent too-skinny cells
    clean_mol_positions = enforce_1d_bound(real_mol_positions, 0.5, 0.5, mode=mode)  # enforce fractional centroids between 0 and 1

    return clean_lattice_lengths, clean_lattice_angles, clean_mol_positions, clean_mol_orientations


def enforce_crystal_system(lattice_lengths, lattice_angles, sg_inds, symmetries_dict):
    """
    enforce physical bounds on cell parameters
    https://en.wikipedia.org/wiki/Crystal_system
    """  # todo double check these limits

    lattices = [symmetries_dict['lattice_type'][int(sg_inds[n])] for n in range(len(sg_inds))]

    pi_tensor = torch.tensor(torch.ones_like(lattice_lengths[0, 0]) * torch.pi)

    fixed_lengths = torch.zeros_like(lattice_lengths)
    fixed_angles = torch.zeros_like(lattice_angles)

    for i in range(len(lattice_lengths)):
        lengths = lattice_lengths[i]
        angles = lattice_angles[i]
        lattice = lattices[i]
        # enforce agreement with crystal system
        if lattice.lower() == 'triclinic':  # anything goes
            fixed_lengths[i] = lengths * 1
            fixed_angles[i] = angles * 1

        elif lattice.lower() == 'monoclinic':  # fix alpha and gamma to pi/2
            fixed_lengths[i] = lengths * 1
            fixed_angles[i] = torch.stack((
                pi_tensor.clone() / 2, angles[1], pi_tensor.clone() / 2,
            ), dim=- 1)
        elif lattice.lower() == 'orthorhombic':  # fix all angles at pi/2
            fixed_lengths[i] = lengths * 1
            fixed_angles[i] = torch.stack((
                pi_tensor.clone() / 2, pi_tensor.clone() / 2, pi_tensor.clone() / 2,
            ), dim=- 1)
        elif lattice.lower() == 'tetragonal':  # fix all angles pi/2 and take the mean of a & b vectors
            mean_tensor = torch.mean(lengths[0:2])
            fixed_lengths[i] = torch.stack((
                mean_tensor, mean_tensor, lengths[2] * 1,
            ), dim=- 1)

            fixed_angles[i] = torch.stack((
                pi_tensor.clone() / 2, pi_tensor.clone() / 2, pi_tensor.clone() / 2,
            ), dim=- 1)

        elif lattice.lower() == 'hexagonal':
            # mean of ab, c is free
            # alpha beta are pi/2, gamma is 2pi/3

            mean_tensor = torch.mean(lengths[0:2])
            fixed_lengths[i] = torch.stack((
                mean_tensor, mean_tensor, lengths[2] * 1,
            ), dim=- 1)

            fixed_angles[i] = torch.stack((
                pi_tensor.clone() / 2, pi_tensor.clone() / 2, pi_tensor.clone() * 3 / 2,
            ), dim=- 1)

        # elif lattice.lower()  == 'trigonal':

        elif lattice.lower() == 'rhombohedral':
            # mean of abc vector lengths
            # mean of all angles

            mean_tensor = torch.mean(lengths)
            fixed_lengths[i] = torch.stack((
                mean_tensor, mean_tensor, mean_tensor,
            ), dim=- 1)

            mean_angle = torch.mean(angles)
            fixed_angles[i] = torch.stack((
                mean_angle, mean_angle, mean_angle,
            ), dim=- 1)

        elif lattice.lower() == 'cubic':  # all angles 90 all lengths equal
            mean_tensor = torch.mean(lengths)
            fixed_lengths[i] = torch.stack((
                mean_tensor, mean_tensor, mean_tensor,
            ), dim=- 1)

            fixed_angles[i] = torch.stack((
                pi_tensor.clone() / 2, pi_tensor.clone() / 2, pi_tensor.clone() / 2,
            ), dim=- 1)
        else:
            print(lattice + ' is not a valid crystal lattice!')
            sys.exit()

    return fixed_lengths, fixed_angles


def decode_to_sph_rotvec(mol_orientations):
    '''
    each angle is predicted with 2 params
    we bound the encodings for theta on 0-1 to restrict the range of theta to [0,pi/2]
    '''
    theta_encoding = F.sigmoid(mol_orientations[:, 0:2])  # restrict to positive quadrant
    real_orientation_theta = components2angle(theta_encoding)
    real_orientation_phi = components2angle(mol_orientations[:, 2:4])  # unrestricted [-pi,pi
    real_orientation_r = components2angle(mol_orientations[:, 4:6]) + torch.pi  # shift from [-pi,pi] to [0, 2pi]  # want vector to have a positive norm

    # clean_mol_orientations = torch.cat((
    #     real_orientation_theta[:, None],
    #     real_orientation_phi[:, None],
    #     real_orientation_r[:, None]
    # ), dim=-1)

    return real_orientation_theta[:, None], real_orientation_phi[:, None], real_orientation_r[:, None]


def get_regression_loss(regressor, data, mean, std):
    predictions = regressor(data.to(regressor.model.device))[:, 0]
    targets = data.y
    return F.smooth_l1_loss(predictions, targets, reduction='none'), predictions.cpu().detach().numpy() * std + mean, targets.cpu().detach().numpy() * std + mean
