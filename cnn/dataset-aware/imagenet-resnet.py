#!/usr/bin/env python
# -*- coding: UTF-8 -*-
# File: imagenet-resnet.py
# Author: Guangrun Wang (wanggrun@mail2.sysu.edu.cn)

import argparse
import os


from tensorpack import logger, QueueInput
from tensorpack.models import *


from tensorpack.callbacks import *
from tensorpack.train import (
    TrainConfig, SyncMultiGPUTrainerReplicated, launch_train_with_config)
from tensorpack.dataflow import FakeData
from tensorpack.tfutils import argscope, get_model_loader
from tensorpack.utils.gpu import get_nr_gpu

from imagenet_utils import (
    fbresnet_augmentor, get_imagenet_dataflow, ImageNetModel,
    eval_on_ILSVRC12)
from resnet_model import (
    preresnet_group, preresnet_basicblock, preresnet_bottleneck,
    resnet_group, resnet_basicblock, resnet_bottleneck, se_resnet_bottleneck,
    resnet_backbone)

from gr_conv import Grconv

class Model(ImageNetModel):
    def __init__(self, depth, mode='resnet'):
        if mode == 'se':
            assert depth >= 50

        self.mode = mode
        basicblock = preresnet_basicblock if mode == 'preact' else resnet_basicblock
        bottleneck = {
            'resnet': resnet_bottleneck,
            'preact': preresnet_bottleneck,
            'se': se_resnet_bottleneck}[mode]
        self.num_blocks, self.block_func = {
            18: ([2, 2, 2, 2], basicblock),
            34: ([3, 4, 6, 3], basicblock),
            50: ([3, 4, 6, 3], bottleneck),
            74: ([3, 4, 16, 3], bottleneck),
            101: ([3, 4, 23, 3], bottleneck),
            152: ([3, 8, 36, 3], bottleneck)
        }[depth]

    def get_logits(self, image):
        with argscope([Conv2D, Grconv, MaxPooling,  AvgPooling, GlobalAvgPooling, BatchNorm], data_format=self.data_format):
            return resnet_backbone(
                image, self.num_blocks,
                preresnet_group if self.mode == 'preact' else resnet_group, self.block_func)


def get_data(name, batch):
    isTrain = name == 'train'
    augmentors = fbresnet_augmentor(isTrain)
    return get_imagenet_dataflow(
        args.data, name, batch, augmentors)


def get_config(model, fake=False):
    start_ = 0
    nr_tower = max(get_nr_gpu(), 1)
    assert args.batch % nr_tower == 0
    batch = args.batch // nr_tower

    logger.info("Running on {} towers. Batch size per tower: {}".format(nr_tower, batch))
    if fake:
        dataset_train = FakeData(
            [[batch, 224, 224, 3], [batch]], 1000, random=False, dtype='uint8')
        callbacks = []
    else:
        dataset_train = get_data('train', batch)
        dataset_val = get_data('val', batch)

        START_LR = 0.1
        BASE_LR = START_LR * (args.batch / 256.0)
        if start_ < 31:
            lr_setting =[(max(30-start_, 0) , BASE_LR * 1e-1), (60 - start_, BASE_LR * 1e-2),(
                90 - start_, BASE_LR * 1e-3), (105 - start_, BASE_LR * 1e-4)]
        elif start_ < 61:
            lr_setting =[(max(60 - start_, 0), BASE_LR * 1e-2),(
                90 - start_, BASE_LR * 1e-3), (105 - start_, BASE_LR * 1e-4)]
        elif start_ < 91:
            lr_setting =[(max(90 - start_, 0), BASE_LR * 1e-3), (105 - start_, BASE_LR * 1e-4)]
        else:
            print('not found learning rate setting!!!!!!!!!!!!!')

        callbacks = [
            ModelSaver(),
            EstimatedTimeLeft(),
            ScheduledHyperParamSetter(
                'learning_rate', lr_setting),
            # TensorPrinter(['tower1/group3/block2/conv2/Abs_0', 'tower1/group3/block2/conv2/Abs_1:0', 'tower1/group3/block2/conv2/Abs_2:0'])
        ]
        if BASE_LR > START_LR:
            callbacks.append(
                ScheduledHyperParamSetter(
                    'learning_rate', [(0, START_LR), (5, BASE_LR)], interp='linear'))

        infs = [ClassificationError('wrong-top1', 'val-error-top1'),
                ClassificationError('wrong-top5', 'val-error-top5')]
        if nr_tower == 1:
            # single-GPU inference with queue prefetch
            callbacks.append(InferenceRunner(QueueInput(dataset_val), infs))
        else:
            # multi-GPU inference (with mandatory queue prefetch)
            callbacks.append(DataParallelInferenceRunner(
                dataset_val, infs, list(range(nr_tower))))

    return TrainConfig(
        model=model,
        dataflow=dataset_train,
        callbacks=callbacks,
        steps_per_epoch=100 if args.fake else 1280000 // args.batch,
        max_epoch=120,
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.')
    parser.add_argument('--data', help='ILSVRC dataset dir')
    parser.add_argument('--load', help='load model')
    parser.add_argument('--fake', help='use fakedata to test or benchmark this model', action='store_true')
    parser.add_argument('--data_format', help='specify NCHW or NHWC',
                        type=str, default='NCHW')
    parser.add_argument('-d', '--depth', help='resnet depth',
                        type=int, default=50, choices=[18, 34, 50, 74, 101, 152])
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('--batch', default=256, type=int,
                        help='total batch size. 32 per GPU gives best accuracy, higher values should be similarly good')
    parser.add_argument('--mode', choices=['resnet', 'preact', 'se'],
                        help='variants of resnet to use', default='resnet')
    parser.add_argument('--log_dir', type=str, default='')
    args = parser.parse_args()

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    model = Model(args.depth, args.mode)
    model.data_format = args.data_format
    if args.eval:
        batch = 128    # something that can run on one gpu
        ds = get_data('val', batch)
        eval_on_ILSVRC12(model, get_model_loader(args.load), ds)
    else:
        if args.fake:
            logger.set_logger_dir(os.path.join('train_log', 'tmp'), 'd')
        else:
            log_foder = '/data0/wangguangrun/log_acnt/imagenet-resnet-%s' % (args.log_dir)
            logger.set_logger_dir(os.path.join(log_foder))

        config = get_config(model, fake=args.fake)
        if args.load:
            config.session_init = get_model_loader(args.load)
        trainer = SyncMultiGPUTrainerReplicated(max(get_nr_gpu(), 1))
        launch_train_with_config(config, trainer)
