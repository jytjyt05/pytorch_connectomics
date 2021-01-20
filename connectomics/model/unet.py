from __future__ import print_function, division
from typing import Optional, List

import torch
import math
import torch.nn as nn
import torch.nn.functional as F

from .block import *
from .utils import get_functional_act, model_init

class UNet3D(nn.Module):
    """3D residual U-Net architecture. This design is flexible in handling both isotropic data and anisotropic data.

    Args:
        block_type (str): the block type at each U-Net stage. Default: ``'residual'``
        in_channel (int): number of input channels. Default: 1
        out_channel (int): number of output channels. Default: 3
        filters (List[int]): number of filters at each U-Net stage. Default: [28, 36, 48, 64, 80]
        is_isotropic (bool): whether the whole model is isotropic. Default: False
        isotropy (List[bool]): specify each U-Net stage is isotropic or anisotropic. All elements will
            be `True` if :attr:`is_isotropic` is `True`. Default: [False, False, False, True, True]
        pad_mode (str): one of ``'zeros'``, ``'reflect'``, ``'replicate'`` or ``'circular'``. Default: ``'replicate'``
        act_mode (str): one of ``'relu'``, ``'leaky_relu'``, ``'elu'``, ``'gelu'``, 
            ``'swish'``, ``'efficient_swish'`` or ``'none'``. Default: ``'relu'``
        norm_mode (str): one of ``'bn'``, ``'sync_bn'`` ``'in'`` or ``'gn'``. Default: ``'bn'``
        init_mode (str): one of ``'xavier'``, ``'kaiming'``, ``'selu'`` or ``'orthogonal'``. Default: ``'orthogonal'``
        pooling (bool): downsample by max-pooling if `True` else using stride. Default: `False`
        output_act (str): activation function for the output layer. Default: ``'sigmoid'``
    """

    block_dict = {
        'residual': residual_block_3d,
        'residual_se': residual_se_block_3d,
    }

    def __init__(self, 
                 block_type = 'residual',
                 in_channel: int = 1, 
                 out_channel: int = 3, 
                 filters: List[int] = [28, 36, 48, 64, 80],
                 is_isotropic: bool = False, 
                 isotropy: List[bool] = [False, False, False, True, True],
                 pad_mode: str = 'replicate', 
                 act_mode: str = 'elu', 
                 norm_mode: str = 'bn', 
                 head_depth: int = 1, 
                 pooling: bool = False,
                 output_act: str = 'sigmoid',
                 **kwargs):
        super().__init__()
        assert len(filters) == len(isotropy)
        if is_isotropic:
            isotropy = [True for _ in len(isotropy)] 

        self.pooling = pooling
        self.output_act = output_act
        self.depth = len(filters)

        block = self.block_dict[block_type]

        shared_kwargs = {
            'pad_mode': pad_mode,
            'act_mode': act_mode,
            'norm_mode': norm_mode}

        # input and output layers
        kernel_size_io, padding_io = self._get_kernal_size(is_isotropic, io_layer=True)
        self.conv_in = conv3d_norm_act(in_channel, filters[0], kernel_size_io, 
            padding=padding_io, **shared_kwargs)
        self.conv_out = conv3d_norm_act(filters[0], out_channel, kernel_size_io, 
            padding=padding_io, pad_mode=pad_mode, act_mode='none', norm_mode='none')
        
        # encoding path
        self.down_layers = []
        for i in range(self.depth):
            kernel_size, padding = self._get_kernal_size(isotropy[i])
            previous = max(0, i-1)
            stride = self._get_stride(isotropy[i], previous, i)
            layer = nn.Sequential(
                self._make_pooling_layer(isotropy[i], previous, i),
                conv3d_norm_act(filters[previous], filters[i], kernel_size, 
                                stride=stride, padding=padding, **shared_kwargs),
                block(filters[i], filters[i], **shared_kwargs))
            self.down_layers.append(layer)
        self.down_layers = nn.ModuleList(self.down_layers)

        # decoding path
        self.up_layers = []
        for j in range(1, self.depth):
            kernel_size, padding = self._get_kernal_size(isotropy[j])
            layer = nn.ModuleList([
                conv3d_norm_act(filters[j], filters[j-1], kernel_size, 
                                padding=padding, **shared_kwargs),
                block(filters[j-1], filters[j-1], **shared_kwargs)])
            self.up_layers.append(layer)
        self.up_layers = nn.ModuleList(self.up_layers)

        #initialization
        model_init(self)

    def forward(self, x):
        x = self.conv_in(x)

        down_x = [None] * (self.depth-1)
        for i in range(self.depth-1):
            x = self.down_layers[i](x)
            down_x[i] = x

        x = self.down_layers[-1](x)

        for j in range(self.depth-1):
            i = self.depth-2-j
            x = self.up_layers[i][0](x)
            x = self._upsample_add(x, down_x[i])
            x = self.up_layers[i][1](x)

        x = self.conv_out(x)
        x = get_functional_act(self.output_act)(x)
        return x

    def _upsample_add(self, x, y):
        """Upsample and add two feature maps.

        When pooling layer is used, the input size is assumed to be even, 
        therefore :attr:`align_corners` is set to `False` to avoid feature 
        mis-match. When downsampling by stride, the input size is assumed 
        to be 2n+1, and :attr:`align_corners` is set to `False`.
        """
        align_corners = False if self.pooling else True
        x = F.interpolate(x, size=y.shape[2:], mode='trilinear',
                          align_corners=align_corners)
        return x + y

    def _get_kernal_size(self, is_isotropic, io_layer=False):
        if io_layer: # kernel and padding size of I/O layers
            if is_isotropic:
                return (5,5,5), (2,2,2)
            return (1,5,5), (0,2,2)
        
        if is_isotropic:
            return (3,3,3), (1,1,1)
        return (1,3,3), (0,1,1)

    def _get_stride(self, is_isotropic, previous, i):
        if self.pooling or previous == i:
            return 1

        return self._get_downsample(is_isotropic)

    def _get_downsample(self, is_isotropic):
        if not is_isotropic:
            return (1,2,2)
        return 2

    def _make_pooling_layer(self, is_isotropic, previous, i):
        if self.pooling and previous != i:
            kernel_size = stride = self._get_downsample(is_isotropic)
            return nn.MaxPool3d(kernel_size, stride)

        return nn.Identity()


class UNet2D(nn.Module):
    """2D residual U-Net architecture.

    Args:
        block_type (str): the block type at each U-Net stage. Default: ``'residual'``
        in_channel (int): number of input channels. Default: 1
        out_channel (int): number of output channels. Default: 3
        filters (List[int]): number of filters at each U-Net stage. Default: [28, 36, 48, 64, 80]
        pad_mode (str): one of ``'zeros'``, ``'reflect'``, ``'replicate'`` or ``'circular'``. Default: ``'replicate'``
        act_mode (str): one of ``'relu'``, ``'leaky_relu'``, ``'elu'``, ``'gelu'``, 
            ``'swish'``, ``'efficient_swish'`` or ``'none'``. Default: ``'relu'``
        norm_mode (str): one of ``'bn'``, ``'sync_bn'`` ``'in'`` or ``'gn'``. Default: ``'bn'``
        init_mode (str): one of ``'xavier'``, ``'kaiming'``, ``'selu'`` or ``'orthogonal'``. Default: ``'orthogonal'``
        pooling (bool): downsample by max-pooling if `True` else using stride. Default: `False`
        output_act (str): activation function for the output layer. Default: ``'sigmoid'``
    """

    block_dict = {
        'residual': residual_block_2d,
        'residual_se': residual_se_block_2d,
    }

    def __init__(self):
        pass
