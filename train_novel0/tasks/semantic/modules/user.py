#!/usr/bin/env python3
# This file is covered by the LICENSE file in the root of this project.

import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import imp
import yaml
import time
from PIL import Image
import __init__ as booger
import collections
import copy
import cv2
import os
import numpy as np

from tasks.semantic.modules.SalsaNext import *
from tasks.semantic.modules.SalsaNextAdf import *
from tasks.semantic.postproc.KNN import KNN
from tasks.semantic.task import get_per_task_classes

from tasks.config import salsanext
import tqdm
from peft import LoraConfig, get_peft_model
from matplotlib import pyplot as plt


class User():
    def __init__(
        self,
        datadir,
        logdir,
        modeldir,
        split,
        uncertainty,
        mc=30,
        is_lora=False,
    ):
        # parameters
        # self.ARCH = ARCH
        # self.DATA = DATA
        self.datadir = datadir
        self.logdir = logdir
        self.modeldir = modeldir
        self.uncertainty = uncertainty
        self.split = split
        self.mc = mc

        from tasks.semantic.dataset.kitti import parser
        # get the data
        is_test = (self.split == "test")
        self.parser = parser.Parser(
            root=self.datadir,
            # datargs=self.DATA,
            # archargs=self.ARCH,
            batch_size=1,
            is_test=is_test,
            gt=True,
            shuffle_train=True,
        )

        # concatenate the encoder and the head
        with torch.no_grad():
            torch.nn.Module.dump_patches = True
            if self.uncertainty:
                self.model = SalsaNextUncertainty(self.parser.get_n_classes())
            else:
                nclasses = get_per_task_classes(
                    salsanext.train.task_name,
                    salsanext.train.task_step,
                )
                self.model = IncrementalSalsaNext(nclasses)
                if is_lora:
                    rank_patterns = []
                    for mod_idx, rank in zip(
                        [1, 2, 3],
                        [64, 64, 32],
                    ):
                        rank_patterns.append({
                            f'upBlock{mod_idx}.conv{i}': rank for i in range(1, 5)
                        })
                    for mod_idx, rank in zip(
                        [4, 5],
                        [128, 128],
                    ):
                        rank_patterns.append({
                            f'resBlock{mod_idx}.conv{i}': rank for i in range(1, 6)
                        })
                    rank_pattern = {}
                    for d in rank_patterns:
                        rank_pattern.update(d)
                    print(f"{rank_pattern = }")
                    lora_config = LoraConfig(
                        r=128,
                        lora_alpha=128,
                        target_modules=list(rank_pattern.keys()),
                        lora_dropout=0.1,
                        bias="lora_only",
                        rank_pattern=rank_pattern,
                        alpha_pattern=rank_pattern,
                    )
                    lora_model = get_peft_model(self.model, lora_config)
                    self.model = lora_model
            state_dict_path = os.path.join(modeldir, "SalsaNext_valid_best")
            w_dict = torch.load(
                state_dict_path,
                map_location=lambda storage, loc: storage,
            )
            print(f"load state dict from {state_dict_path}")
            # self.model = nn.DataParallel(self.model)
            self.model.load_state_dict(w_dict['state_dict'], strict=True)

        # use knn post processing?
        self.post = None
        if salsanext.post.KNN.use:
            self.post = KNN(
                salsanext.post.KNN.params,
                self.parser.get_n_classes(),
            )

        # GPU?
        self.gpu = False
        self.model_single = self.model
        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu")
        print("Infering in device: ", self.device)
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            cudnn.benchmark = True
            cudnn.fastest = True
            self.gpu = True
            self.model.cuda()

    def infer(self):
        cnn = []
        knn = []
        if self.split == None:

            self.infer_subset(
                loader=self.parser.get_train_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn, knn=knn,
            )

            # do valid set
            self.infer_subset(
                loader=self.parser.get_valid_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn, knn=knn,
            )
            # do test set
            self.infer_subset(
                loader=self.parser.get_test_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn, knn=knn,
            )

        elif self.split == 'valid':
            self.infer_subset(
                loader=self.parser.get_valid_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn, knn=knn,
            )
        elif self.split == 'train':
            self.infer_subset(
                loader=self.parser.get_train_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn, knn=knn,
            )
        else:
            self.infer_subset(
                loader=self.parser.get_test_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn, knn=knn,
            )
        print("Mean CNN inference time:{}\t std:{}".format(
            np.mean(cnn), np.std(cnn)))
        print("Mean KNN inference time:{}\t std:{}".format(
            np.mean(knn), np.std(knn)))
        print("Total Frames:{}".format(len(cnn)))
        print("Finished Infering")

        return

    def infer_subset(self, loader, to_orig_fn, cnn, knn):
        # switch to evaluate mode
        if not self.uncertainty:
            self.model.eval()
        total_time = 0
        total_frames = 0
        # empty the cache to infer in high res
        if self.gpu:
            torch.cuda.empty_cache()

        with torch.no_grad():
            end = time.time()

            for i, (proj_in, proj_mask, _, _, path_seq, path_name, p_x, p_y, proj_range, unproj_range, _, _, _, _, npoints) in enumerate(tqdm.tqdm(loader)):
                print(f"epoch : {i}")
                # first cut to rela size (batch size one allows it)
                npoints = npoints.min()

                p_x = p_x[0, :npoints]
                p_y = p_y[0, :npoints]
                proj_range = proj_range[0, :npoints]
                unproj_range = unproj_range[0, :npoints]
                path_seq = path_seq[0]
                path_name = path_name[0]

                if self.gpu:
                    proj_in = proj_in.cuda()
                    p_x = p_x.cuda()
                    p_y = p_y.cuda()
                    if self.post:
                        proj_range = proj_range.cuda()
                        unproj_range = unproj_range.cuda()

                # compute output
                if self.uncertainty:
                    proj_output_r, log_var_r = self.model(proj_in)
                    for i in range(self.mc):
                        log_var, proj_output = self.model(proj_in)
                        log_var_r = torch.cat((log_var, log_var_r))
                        proj_output_r = torch.cat((proj_output, proj_output_r))

                    proj_output2, log_var2 = self.model(proj_in)
                    proj_output = proj_output_r.var(
                        dim=0, keepdim=True).mean(dim=1)
                    log_var2 = log_var_r.mean(dim=0, keepdim=True).mean(dim=1)
                    if self.post:
                        # knn postproc
                        unproj_argmax = self.post(
                            proj_range,
                            unproj_range,
                            proj_argmax,
                            p_x,
                            p_y,
                        )
                    else:
                        # put in original pointcloud using indexes
                        unproj_argmax = proj_argmax[p_y, p_x]

                    # measure elapsed time
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    frame_time = time.time() - end
                    print("Infered seq", path_seq, "scan", path_name,
                          "in", frame_time, "sec")
                    total_time += frame_time
                    total_frames += 1
                    end = time.time()

                    # save scan
                    # get the first scan in batch and project scan
                    pred_np = unproj_argmax.cpu().numpy()
                    pred_np = pred_np.reshape((-1)).astype(np.int32)

                    # log_var2 = log_var2[0][p_y, p_x]
                    # log_var2 = log_var2.cpu().numpy()
                    # log_var2 = log_var2.reshape((-1)).astype(np.float32)

                    log_var2 = log_var2[0][p_y, p_x]
                    log_var2 = log_var2.cpu().numpy()
                    log_var2 = log_var2.reshape((-1)).astype(np.float32)
                    # assert proj_output.reshape((-1)).shape == log_var2.reshape((-1)).shape == pred_np.reshape((-1)).shape

                    # map to original label
                    pred_np = to_orig_fn(pred_np)

                    # save scan
                    path = os.path.join(
                        self.logdir, "sequences",
                        path_seq, "predictions", path_name,
                    )
                    pred_np.tofile(path)

                    path = os.path.join(
                        self.logdir, "sequences",
                        path_seq, "log_var", path_name,
                    )
                    if not os.path.exists(
                        os.path.join(self.logdir, "sequences",
                                     path_seq, "log_var"),
                    ):
                        os.makedirs(
                            os.path.join(self.logdir, "sequences",
                                         path_seq, "log_var"),
                        )
                    log_var2.tofile(path)

                    proj_output = proj_output[0][p_y, p_x]
                    proj_output = proj_output.cpu().numpy()
                    proj_output = proj_output.reshape((-1)).astype(np.float32)

                    path = os.path.join(
                        self.logdir, "sequences",
                        path_seq, "uncert", path_name,
                    )
                    if not os.path.exists(
                        os.path.join(self.logdir, "sequences",
                                     path_seq, "uncert"),
                    ):
                        os.makedirs(
                            os.path.join(self.logdir, "sequences",
                                         path_seq, "uncert"),
                        )
                    proj_output.tofile(path)

                    print(total_time / total_frames)
                else:
                    proj_output, logits, decode_result = self.model(proj_in)
                    proj_argmax = proj_output[0].argmax(dim=0)
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    res = time.time() - end
                    print("Network seq", path_seq, "scan", path_name,
                          "in", res, "sec")
                    end = time.time()
                    cnn.append(res)

                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    res = time.time() - end
                    print("Network seq", path_seq, "scan", path_name,
                          "in", res, "sec")
                    end = time.time()
                    cnn.append(res)

                    if self.post:
                        # knn postproc
                        unproj_argmax = self.post(
                            proj_range,
                            unproj_range,
                            proj_argmax,
                            p_x,
                            p_y,
                        )
                    else:
                        # put in original pointcloud using indexes
                        unproj_argmax = proj_argmax[p_y, p_x]

                    # measure elapsed time
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                    res = time.time() - end
                    print("KNN Infered seq", path_seq, "scan", path_name,
                          "in", res, "sec")
                    knn.append(res)
                    end = time.time()

                    # save scan
                    # get the first scan in batch and project scan
                    pred_np = unproj_argmax.cpu().numpy()
                    pred_np = pred_np.reshape((-1)).astype(np.int32)

                    # map to original label
                    pred_np = to_orig_fn(pred_np)

                    # save scan
                    path = os.path.join(self.logdir, "sequences",
                                        path_seq, "predictions", path_name)
                    pred_np.tofile(path)
                    print(f"saving to {path}")
