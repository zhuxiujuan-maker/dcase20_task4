# -*- coding: utf-8 -*-
#########################################################################
# Initial software
# Copyright Nicolas Turpault, Romain Serizel, Justin Salamon, Ankit Parag Shah, 2019, v1.0
# This software is distributed under the terms of the License MIT
#########################################################################
import os
from os import path as osp

import psds_eval
import scipy
from dcase_util.data import ProbabilityEncoder
import sed_eval
import numpy as np
import pandas as pd
import torch
from psds_eval import PSDSEval

import config as cfg
from utilities.Logger import create_logger
from utilities.utils import ManyHotEncoder, to_cuda_if_available

logger = create_logger(__name__)


def get_f_measure_by_class(torch_model, nb_tags, dataloader_, thresholds_=None):
    """ get f measure for each class given a model and a generator of data (batch_x, y)

    Args:
        torch_model : Model, model to get predictions, forward should return weak and strong predictions
        nb_tags : int, number of classes which are represented
        dataloader_ : generator, data generator used to get f_measure
        thresholds_ : int or list, thresholds to apply to each class to binarize probabilities

    Returns:
        macro_f_measure : list, f measure for each class

    """
    if torch.cuda.is_available():
        torch_model = torch_model.cuda()

    # Calculate external metrics
    tp = np.zeros(nb_tags)
    tn = np.zeros(nb_tags)
    fp = np.zeros(nb_tags)
    fn = np.zeros(nb_tags)
    for counter, (batch_x, y) in enumerate(dataloader_):
        if torch.cuda.is_available():
            batch_x = batch_x.cuda()

        pred_strong, pred_weak = torch_model(batch_x)
        pred_weak = pred_weak.cpu().data.numpy()
        labels = y.numpy()

        # Used only with a model predicting only strong outputs
        if len(pred_weak.shape) == 3:
            # average data to have weak labels
            pred_weak = np.max(pred_weak, axis=1)

        if len(labels.shape) == 3:
            labels = np.max(labels, axis=1)
            labels = ProbabilityEncoder().binarization(labels,
                                                       binarization_type="global_threshold",
                                                       threshold=0.5)

        if thresholds_ is None:
            binarization_type = 'global_threshold'
            thresh = 0.5
        else:
            binarization_type = "class_threshold"
            assert type(thresholds_) is list
            thresh = thresholds_

        batch_predictions = ProbabilityEncoder().binarization(pred_weak,
                                                              binarization_type=binarization_type,
                                                              threshold=thresh,
                                                              time_axis=0
                                                              )

        tp_, fp_, fn_, tn_ = intermediate_at_measures(labels, batch_predictions)
        tp += tp_
        fp += fp_
        fn += fn_
        tn += tn_

    macro_f_measure = np.zeros(nb_tags)
    mask_f_score = 2 * tp + fp + fn != 0
    macro_f_measure[mask_f_score] = 2 * tp[mask_f_score] / (2 * tp + fp + fn)[mask_f_score]

    return macro_f_measure


def intermediate_at_measures(encoded_ref, encoded_est):
    """ Calculate true/false - positives/negatives.

    Args:
        encoded_ref: np.array, the reference array where a 1 means the label is present, 0 otherwise
        encoded_est: np.array, the estimated array, where a 1 means the label is present, 0 otherwise

    Returns:
        tuple
        number of (true positives, false positives, false negatives, true negatives)

    """
    tp = (encoded_est + encoded_ref == 2).sum(axis=0)
    fp = (encoded_est - encoded_ref == 1).sum(axis=0)
    fn = (encoded_ref - encoded_est == 1).sum(axis=0)
    tn = (encoded_est + encoded_ref == 0).sum(axis=0)
    return tp, fp, fn, tn


def get_event_list_current_file(df, fname):
    """
    Get list of events for a given filename
    :param df: pd.DataFrame, the dataframe to search on
    :param fname: the filename to extract the value from the dataframe
    :return: list of events (dictionaries) for the given filename
    """
    event_file = df[df["filename"] == fname]
    if len(event_file) == 1:
        if pd.isna(event_file["event_label"].iloc[0]):
            event_list_for_current_file = [{"filename": fname}]
        else:
            event_list_for_current_file = event_file.to_dict('records')
    else:
        event_list_for_current_file = event_file.to_dict('records')

    return event_list_for_current_file


def event_based_evaluation_df(reference, estimated, t_collar=0.200, percentage_of_length=0.2):
    """
    Calculate EventBasedMetric given a reference and estimated dataframe
    :param reference: pd.DataFrame containing "filename" "onset" "offset" and "event_label" columns which describe the
    reference events
    :param estimated: pd.DataFrame containing "filename" "onset" "offset" and "event_label" columns which describe the
    estimated events to be compared with reference
    :return: sed_eval.sound_event.EventBasedMetrics with the scores
    """

    evaluated_files = reference["filename"].unique()

    classes = []
    classes.extend(reference.event_label.dropna().unique())
    classes.extend(estimated.event_label.dropna().unique())
    classes = list(set(classes))

    event_based_metric = sed_eval.sound_event.EventBasedMetrics(
        event_label_list=classes,
        t_collar=t_collar,
        percentage_of_length=percentage_of_length,
        empty_system_output_handling='zero_score'
    )

    for fname in evaluated_files:
        reference_event_list_for_current_file = get_event_list_current_file(reference, fname)
        estimated_event_list_for_current_file = get_event_list_current_file(estimated, fname)

        event_based_metric.evaluate(
            reference_event_list=reference_event_list_for_current_file,
            estimated_event_list=estimated_event_list_for_current_file,
        )

    return event_based_metric


def segment_based_evaluation_df(reference, estimated, time_resolution=1.):
    evaluated_files = reference["filename"].unique()

    classes = []
    classes.extend(reference.event_label.dropna().unique())
    classes.extend(estimated.event_label.dropna().unique())
    classes = list(set(classes))

    segment_based_metric = sed_eval.sound_event.SegmentBasedMetrics(
        event_label_list=classes,
        time_resolution=time_resolution
    )

    for fname in evaluated_files:
        reference_event_list_for_current_file = get_event_list_current_file(reference, fname)
        estimated_event_list_for_current_file = get_event_list_current_file(estimated, fname)

        segment_based_metric.evaluate(
            reference_event_list=reference_event_list_for_current_file,
            estimated_event_list=estimated_event_list_for_current_file
        )

    return segment_based_metric


def macro_f_measure(tp, fp, fn):
    """ From intermediates measures, give the macro F-measure

    Args:
        tp: int, number of true positives
        fp: int, number of false positives
        fn: int, number of true negatives

    Returns:
        float
        The macro F-measure
    """
    macro_f_score = np.zeros(tp.shape[-1])
    mask_f_score = 2 * tp + fp + fn != 0
    macro_f_score[mask_f_score] = 2 * tp[mask_f_score] / (2 * tp + fp + fn)[mask_f_score]
    return macro_f_score


def get_predictions(model, valid_dataloader, decoder, pooling_time_ratio=1, median_window=1, save_predictions=None):
    prediction_df = pd.DataFrame()
    for i, ((input_data, _), indexes) in enumerate(valid_dataloader):
        indexes = indexes.numpy()
        input_data = to_cuda_if_available(input_data)

        pred_strong, _ = model(input_data)
        pred_strong = pred_strong.cpu()
        pred_strong = pred_strong.detach().numpy()
        if i == 0:
            logger.debug(pred_strong)

        for j, pred_strong_it in enumerate(pred_strong):
            pred_strong_it = ProbabilityEncoder().binarization(pred_strong_it, binarization_type="global_threshold",
                                                               threshold=0.5)
            pred_strong_it = scipy.ndimage.filters.median_filter(pred_strong_it, (median_window, 1))
            pred = decoder(pred_strong_it)
            pred = pd.DataFrame(pred, columns=["event_label", "onset", "offset"])
            pred["filename"] = valid_dataloader.dataset.filenames.iloc[indexes[j]]
            prediction_df = prediction_df.append(pred)

            if i == 0 and j == 0:
                logger.debug("predictions: \n{}".format(pred))
                logger.debug("predictions strong: \n{}".format(pred_strong_it))

    # In seconds
    prediction_df.loc[:, "onset"] = prediction_df.onset * pooling_time_ratio / (cfg.sample_rate / cfg.hop_length)
    prediction_df.loc[:, "offset"] = prediction_df.offset * pooling_time_ratio / (cfg.sample_rate / cfg.hop_length)
    prediction_df = prediction_df.reset_index(drop=True)
    if save_predictions is not None:
        dir_to_create = osp.dirname(save_predictions)
        if dir_to_create != "":
            os.makedirs(dir_to_create, exist_ok=True)
        logger.info("Saving predictions at: {}".format(save_predictions))
        prediction_df.to_csv(save_predictions, index=False, sep="\t", float_format="%.3f")
    return prediction_df


def psds_results(predictions, gtruth_df, gtruth_durations):
    dtc_threshold = 0.5
    gtc_threshold = 0.5
    cttc_threshold = 0.3
    # Instantiate PSDSEval
    psds = PSDSEval(dtc_threshold, gtc_threshold, cttc_threshold,
                    ground_truth=gtruth_df, metadata=gtruth_durations)

    psds.add_operating_point(predictions)
    psds_score = psds.psds(alpha_ct=0, alpha_st=0, max_efpr=100)
    print(f"\nPSD-Score (0, 0, 100): {psds_score.value:.5f}")
    psds_score = psds.psds(alpha_ct=1, alpha_st=0, max_efpr=100)
    print(f"\nPSD-Score (1, 0, 100): {psds_score.value:.5f}")
    psds_score = psds.psds(alpha_ct=0, alpha_st=1, max_efpr=100)
    print(f"\nPSD-Score (0, 1, 100): {psds_score.value:.5f}")


def compute_sed_eval_metrics(predictions, groundtruth):
    metric_event = event_based_evaluation_df(groundtruth, predictions, t_collar=0.200,
                                             percentage_of_length=0.2)
    metric_segment = segment_based_evaluation_df(groundtruth, predictions, time_resolution=1.)
    logger.info(metric_event)
    logger.info(metric_segment)

    return metric_event


def format_df(df, mhe):
    def join_labels(x):
        return pd.Series(dict(filename = x['filename'].iloc[0],
                              event_label = mhe.encode_weak(x["event_label"].drop_duplicates().dropna().tolist())))

    if "onset" in df.columns or "offset" in df.columns:
        df = df.groupby("filename", as_index=False).apply(join_labels)
    return df


def audio_tagging_results(reference, estimated):
    classes = []
    if "event_label" in reference.columns:
        classes.extend(reference.event_label.dropna().unique())
        classes.extend(estimated.event_label.dropna().unique())
        classes = list(set(classes))
        mhe = ManyHotEncoder(classes)
        reference = format_df(reference, mhe)
        estimated = format_df(estimated, mhe)
    else:
        classes.extend(reference.event_labels.str.split(',', expand=True).unstack().dropna().unique())
        classes.extend(estimated.event_labels.str.split(',', expand=True).unstack().dropna().unique())
        classes = list(set(classes))
        mhe = ManyHotEncoder(classes)

    matching = reference.merge(estimated, how='outer', on="filename", suffixes=["_ref", "_pred"])

    def na_values(val):
        if type(val) is np.ndarray:
            return val
        if pd.isna(val):
            return np.zeros(len(classes))
        return val

    if not estimated.empty:
        matching.event_label_pred = matching.event_label_pred.apply(na_values)
        matching.event_label_ref = matching.event_label_ref.apply(na_values)

        tp, fp, fn, tn = intermediate_at_measures(np.array(matching.event_label_ref.tolist()),
                                                  np.array(matching.event_label_pred.tolist()))
        macro_res = macro_f_measure(tp, fp, fn)
    else:
        macro_res = np.zeros(len(classes))

    results_serie = pd.DataFrame(macro_res, index=mhe.labels)
    return results_serie[0]
