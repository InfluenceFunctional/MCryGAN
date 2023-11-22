import numpy as np
from _plotly_utils.colors import sample_colorscale
from plotly import graph_objects as go
from plotly.subplots import make_subplots
from sklearn.metrics import confusion_matrix, roc_auc_score, f1_score

from classify_lammps_trajs.NICOAM_constants import defect_names, nic_ordered_class_names, form2index, index2form
import plotly
from scipy.ndimage import gaussian_filter1d


def embedding_fig(results_dict, num_samples):

    sample_inds = np.random.choice(num_samples, size=min(1000, num_samples), replace=False)
    from sklearn.manifold import TSNE
    embedding = TSNE(n_components=2, learning_rate='auto', verbose=1, n_iter=5000,
                     init='pca', perplexity=30).fit_transform(results_dict['Latents'][sample_inds])

    # target_colors = n_colors('rgb(250,50,5)', 'rgb(5,120,200)', 10, colortype='rgb')
    target_colors = (
        'rgb(229, 134, 6)', 'rgb(93, 105, 177)', 'rgb(82, 188, 163)', 'rgb(153, 201, 69)', 'rgb(204, 97, 176)', 'rgb(36, 121, 108)', 'rgb(218, 165, 27)', 'rgb(47, 138, 196)', 'rgb(118, 78, 159)', 'rgb(237, 100, 90)', 'rgb(165, 170, 153)')
    # sample_colorscale('hsv', 10)
    linewidths = [0, 1]
    linecolors = [None, 'DarkSlateGrey']
    symbols = ['circle', 'diamond', 'square']

    fig = go.Figure()
    for temp_ind, temperature in enumerate([100, 350, 950]):
        for t_ind in range(10):  # todo switch to sum over forms
            for d_ind in range(len(defect_names)):
                inds = np.argwhere((results_dict['Temperature'][sample_inds] == temperature) *
                                   (results_dict['Targets'][sample_inds] == t_ind) *
                                   (results_dict['Defects'][sample_inds] == d_ind)
                                   )[:, 0]

                fig.add_trace(go.Scattergl(x=embedding[inds, 0], y=embedding[inds, 1],
                                           mode='markers',
                                           marker_color=target_colors[t_ind],
                                           marker_symbol=symbols[temp_ind],
                                           marker_line_width=linewidths[d_ind],
                                           marker_line_color=linecolors[d_ind],
                                           legendgroup=nic_ordered_class_names[t_ind],
                                           name=nic_ordered_class_names[t_ind],  # + ', ' + defect_names[d_ind],# + ', ' + str(temperature) + 'K',
                                           showlegend=True if (temperature == 100 or temperature == 950) and d_ind == 0 else False,
                                           opacity=0.75))

    return fig


def form_accuracy_fig(results_dict):
    scores = {}
    fig = make_subplots(cols=3, rows=1, subplot_titles=['100K', '350K', '950K'], y_title="True Forms", x_title="Predicted Forms")
    for temp_ind, temperature in enumerate([100, 350, 950]):
        inds = np.argwhere(results_dict['Temperature'] == temperature)[:, 0]
        probs = results_dict['Type_Prediction'][inds]
        predicted_class = np.argmax(probs, axis=1)
        true_labels = results_dict['Targets'][inds]

        if temperature == 950:
            true_labels = np.ones_like(true_labels)
            predicted_class = np.asarray(predicted_class == 9).astype(int)
            probs_0 = probs[:, -2:]
            probs_0[:, 0] = probs[:, :-1].sum(1)
            probs = probs_0

        cmat = confusion_matrix(true_labels, predicted_class, normalize='true')

        try:
            auc = roc_auc_score(true_labels, probs, multi_class='ovo')
        except ValueError:
            auc = 1

        f1 = f1_score(true_labels, predicted_class, average='micro')

        if temperature == 950:
            fig.add_trace(go.Heatmap(z=cmat, x=['Ordered', 'Disordered'], y=['Ordered', 'Disordered'],
                                     text=np.round(cmat, 2), texttemplate="%{text:.2g}", showscale=False),
                          row=1, col=temp_ind + 1)
        else:
            fig.add_trace(go.Heatmap(z=cmat, x=nic_ordered_class_names, y=nic_ordered_class_names,
                                     text=np.round(cmat, 2), texttemplate="%{text:.2g}", showscale=False),
                          row=1, col=temp_ind + 1)

        fig.layout.annotations[temp_ind].update(text=f"{temperature}K: ROC AUC={auc:.2f}, F1={f1:.2f}")

        scores[str(temperature) + '_F1'] = f1
        scores[str(temperature) + '_ROC_AUC'] = auc

    return fig, scores


def defect_accuracy_fig(results_dict):
    scores = {}
    fig = make_subplots(cols=3, rows=1, subplot_titles=['100K', '350K', '950K'], y_title="True Defects", x_title="Predicted Defects")
    for temp_ind, temperature in enumerate([100, 350, 950]):
        inds = np.argwhere(results_dict['Temperature'] == temperature)[:, 0]
        probs = results_dict['Defect_Prediction'][inds]
        predicted_class = np.argmax(probs, axis=1)
        true_labels = results_dict['Defects'][inds]

        cmat = confusion_matrix(true_labels, predicted_class, normalize='true')

        try:
            auc = roc_auc_score(true_labels, probs, multi_class='ovo')
        except ValueError:
            auc = 1

        f1 = f1_score(true_labels, predicted_class, average='micro')

        fig.add_trace(go.Heatmap(z=cmat, x=defect_names, y=defect_names,
                                 text=np.round(cmat, 2), texttemplate="%{text:.2g}", showscale=False),
                      row=1, col=temp_ind + 1)

        fig.layout.annotations[temp_ind].update(text=f"{temperature}K: ROC AUC={auc:.2f}, F1={f1:.2f}")

        scores[str(temperature) + '_F1'] = f1
        scores[str(temperature) + '_ROC_AUC'] = auc

    return fig, scores


def all_accuracy_fig(results_dict):  # todo fix class ordering
    scores = {}
    fig = make_subplots(cols=2, rows=1, subplot_titles=['100K', '350K'], y_title="True Class", x_title="Predicted Class")
    for temp_ind, temperature in enumerate([100, 350]):
        inds = np.argwhere(results_dict['Temperature'] == temperature)[:, 0]
        defect_probs = results_dict['Defect_Prediction'][inds]
        form_probs = results_dict['Type_Prediction'][inds]
        probs = np.stack([np.outer(defect_probs[ind], form_probs[ind]).T.reshape(len(nic_ordered_class_names) * len(defect_names)) for ind in range(len(form_probs))])

        predicted_class = np.argmax(probs, axis=1)
        true_defects = results_dict['Defects'][inds]
        true_forms = results_dict['Targets'][inds]

        true_labels = np.asarray([target * 2 + defect for target, defect in zip(true_forms, true_defects)])

        combined_names = [class_name + ' ' + defect_name for class_name in nic_ordered_class_names for defect_name in defect_names]

        cmat = confusion_matrix(true_labels, predicted_class, normalize='true')

        try:
            auc = roc_auc_score(true_labels, probs, multi_class='ovo')
        except ValueError:
            auc = 1

        f1 = f1_score(true_labels, predicted_class, average='micro')

        fig.add_trace(go.Heatmap(z=cmat, x=combined_names, y=combined_names,
                                 text=np.round(cmat, 2), texttemplate="%{text:.2g}", showscale=False),
                      row=1, col=temp_ind + 1)

        fig.layout.annotations[temp_ind].update(text=f"{temperature}K: ROC AUC={auc:.2f}, F1={f1:.2f}")

        scores[str(temperature) + '_F1'] = f1
        scores[str(temperature) + '_ROC_AUC'] = auc

    return fig, scores


def classifier_trajectory_analysis_fig(sorted_molwise_results_dict, time_steps):

    """trajectory analysis figure"""
    pred_frac_traj = np.zeros((len(time_steps), 10))
    pred_frac_traj_in = np.zeros((len(time_steps), 10))
    pred_frac_traj_out = np.zeros((len(time_steps), 10))
    pred_confidence_traj = np.zeros(len(time_steps))
    pred_confidence_traj_in = np.zeros(len(time_steps))
    pred_confidence_traj_out = np.zeros(len(time_steps))

    def get_prediction_confidence(p1):
        return -np.log10(p1.prod(1)) / len(p1[0]) / np.log10(len(p1[0]))

    for ind, probs in enumerate(sorted_molwise_results_dict['Molecule_Type_Prediction']):
        inside_probs = probs[np.argwhere(sorted_molwise_results_dict['Molecule_Coordination_Numbers'][ind] > 20)][:, 0]
        outside_probs = probs[np.argwhere(sorted_molwise_results_dict['Molecule_Coordination_Numbers'][ind] < 20)][:, 0]

        pred = np.argmax(probs, axis=-1)
        inside_pred = np.argmax(inside_probs, axis=-1)
        outside_pred = np.argmax(outside_probs, axis=-1)

        pred_confidence_traj[ind] = probs.max(1).mean()  # get_prediction_confidence(probs).mean()
        pred_confidence_traj_in[ind] = inside_probs.max(1).mean()  # get_prediction_confidence(inside_probs).mean()
        pred_confidence_traj_out[ind] = outside_probs.max(1).mean()  # get_prediction_confidence((outside_probs)).mean()

        uniques, counts = np.unique(pred, return_counts=True)
        count_sum = sum(counts)
        for thing, count in zip(uniques, counts):
            pred_frac_traj[ind, thing] = count / count_sum

        uniques, counts = np.unique(inside_pred, return_counts=True)
        count_sum = sum(counts)
        for thing, count in zip(uniques, counts):
            pred_frac_traj_in[ind, thing] = count / count_sum

        uniques, counts = np.unique(outside_pred, return_counts=True)
        count_sum = sum(counts)
        for thing, count in zip(uniques, counts):
            pred_frac_traj_out[ind, thing] = count / count_sum

    traj_dict = {'overall_fraction': pred_frac_traj,
                 'inside_fraction': pred_frac_traj_in,
                 'outside_fraction': pred_frac_traj_out,
                 'overall_confidence': pred_confidence_traj,
                 'inside_confidence': pred_confidence_traj_in,
                 'outside_confidence': pred_confidence_traj_out}

    colors = plotly.colors.DEFAULT_PLOTLY_COLORS
    sigma = 5
    fig = make_subplots(cols=3, rows=1, subplot_titles=['All Molecules', 'Core', 'Surface'], x_title="Time (ns)", y_title="Form Fraction")
    for ind in range(10):
        fig.add_trace(go.Scattergl(x=time_steps / 1000000,
                                   y=gaussian_filter1d(pred_frac_traj[:, ind], sigma),
                                   name=nic_ordered_class_names[ind],
                                   legendgroup=nic_ordered_class_names[ind],
                                   marker_color=colors[ind]),
                      row=1, col=1)
        fig.add_trace(go.Scattergl(x=time_steps / 1000000,
                                   y=gaussian_filter1d(pred_frac_traj_in[:, ind], sigma),
                                   name=nic_ordered_class_names[ind],
                                   legendgroup=nic_ordered_class_names[ind],
                                   showlegend=False,
                                   marker_color=colors[ind]),
                      row=1, col=2)
        fig.add_trace(go.Scattergl(x=time_steps / 1000000,
                                   y=gaussian_filter1d(pred_frac_traj_out[:, ind], sigma),
                                   name=nic_ordered_class_names[ind],
                                   legendgroup=nic_ordered_class_names[ind],
                                   showlegend=False,
                                   marker_color=colors[ind]),
                      row=1, col=3)

    fig.add_trace(go.Scattergl(x=time_steps / 1000000,
                               y=gaussian_filter1d(pred_confidence_traj[:], sigma),
                               name="Confidence",
                               marker_color='Grey'),
                  row=1, col=1)
    fig.add_trace(go.Scattergl(x=time_steps / 1000000,
                               y=gaussian_filter1d(pred_confidence_traj_in[:], sigma),
                               name="Confidence",
                               showlegend=False,
                               marker_color='Grey'),
                  row=1, col=2)
    fig.add_trace(go.Scattergl(x=time_steps / 1000000,
                               y=gaussian_filter1d(pred_confidence_traj_out[:], sigma),
                               name="Confidence",
                               showlegend=False,
                               marker_color='Grey'),
                  row=1, col=3)

    return fig, traj_dict


def check_for_extra_values(row, extra_axes, extra_values):
    if extra_axes is not None:
        bools = []
        for iv, axis in enumerate(extra_axes):
            bools.append(extra_values[iv] == row[axis])
        return all(bools)
    else:
        return True


def collate_property_over_multiple_runs(target_property, results_df, xaxis, xaxis_title, yaxis, yaxis_title, unique_structures, extra_axes=None, extra_axes_values=None, take_mean=False):
    n_samples = np.zeros((len(unique_structures), len(xaxis), len(yaxis)))

    for iX, xval in enumerate(xaxis):
        for iC, struct in enumerate(unique_structures):
            for iY, yval in enumerate(yaxis):
                for ii, row in results_df.iterrows():
                    if row['structure_identifier'] == struct:
                        if row[xaxis_title] == xval:
                            if row[yaxis_title] == yval:
                                if check_for_extra_values(row, extra_axes, extra_axes_values):
                                    try:
                                        aa = row[target_property]  # see if it's non-empty
                                        n_samples[iC, iX, iY] += 1
                                    except:
                                        pass

    shift_heatmap = np.zeros((len(unique_structures), len(xaxis), len(yaxis)))
    for iX, xval in enumerate(xaxis):
        for iC, struct in enumerate(unique_structures):
            for iY, yval in enumerate(yaxis):
                for ii, row in results_df.iterrows():
                    if row['structure_identifier'] == struct:
                        if row[xaxis_title] == xval:
                            if row[yaxis_title] == yval:
                                if check_for_extra_values(row, extra_axes, extra_axes_values):
                                    try:
                                        if take_mean:
                                            shift_heatmap[iC, iX, iY] += row[target_property].mean() / n_samples[iC, iX, iY]  # take mean over seeds
                                        else:
                                            shift_heatmap[iC, iX, iY] += row[target_property] / n_samples[iC, iX, iY]
                                    except:
                                        shift_heatmap[iC, iX, iY] = 0

    return shift_heatmap, n_samples


def plot_classifier_pies(results_df, xaxis_title, yaxis_title, class_names, extra_axes=None, extra_axes_values=None):
    xaxis = np.unique(results_df[xaxis_title])
    yaxis = np.unique(results_df[yaxis_title])
    unique_structures = np.unique(results_df['structure_identifier'])
    heatmaps, samples = [], []

    for classo in class_names:
        shift_heatmap, n_samples = collate_property_over_multiple_runs(
            classo, results_df, xaxis, xaxis_title, yaxis, yaxis_title, unique_structures,
            extra_axes=extra_axes, extra_axes_values=extra_axes_values, take_mean=False)
        heatmaps.append(shift_heatmap)
        samples.append(n_samples)
    heatmaps = np.stack(heatmaps)
    heatmaps = np.transpose(heatmaps, axes=(0, 1, 3, 2))
    samples = np.stack(samples)

    xlen = len(xaxis)
    ylen = len(yaxis)

    for form_ind, form in enumerate(unique_structures):
        titles = []
        ind = 0
        for i in range(ylen):
            for j in range(xlen):
                row = xlen - ind // ylen - 1
                col = ind % xlen
                titles.append(f"{xaxis_title}={xaxis[j]} <br> {yaxis_title}={yaxis[i]}")
                ind += 1

        fig = make_subplots(rows=ylen, cols=xlen, subplot_titles=titles,
                            specs=[[{"type": "domain"} for _ in range(xlen)] for _ in range(ylen)])

        ind = 0
        for i in range(xlen):
            for j in range(ylen):
                row = j + 1
                col = i + 1
                fig.add_trace(go.Pie(labels=class_names, values=heatmaps[:, form_ind, j, i], sort=False
                                     ),
                              row=row, col=col)
                ind += 1
        fig.update_traces(hoverinfo='label+percent+name', textinfo='none', hole=0.4)
        fig.layout.legend.traceorder = 'normal'
        fig.update_layout(title=form + " Clusters Classifier Outputs")
        fig.update_annotations(font_size=10)

        if extra_axes is not None:
            property_name = form + ' ' + str(extra_axes) + ' ' + str(extra_axes_values)
        else:
            property_name = form
        fig.update_layout(title=property_name)
        fig.show(renderer="browser")
        fig.write_image(form + "_classifier_pies.png")
