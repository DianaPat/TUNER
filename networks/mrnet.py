import numpy as np
import torch
import torch.distributions as dist
from torch import nn
from torch.nn.parameter import Parameter
from typing import Iterator, Sequence, Union
from copy import deepcopy
from itertools import product

from .siren import SineLayer
from utils import create_clamps


class Initializer:
    def __init__(self, hyper, init_freqs=[None, None], bias_init=True,
                 init_W=True, sample_path=None):
        self.hyper = hyper

        if hyper['period']:
            self.factor = 2 * torch.pi / hyper['period']
            self.initialize_first_layer(init_freqs, sample_path=sample_path)
        else:
            self.factor = torch.pi / hyper['omega_0']
            self.initialize_first_layer(init_freqs, sample_path=sample_path)
            # self.model = MRFactory.from_dict(self.hyper)
        if bias_init:
            self.initialize_bias()
        if self.hyper['bounds'][0] and init_W:
            self.initialize_middle_layer()
        self._input_frequencies()

    def initialize_first_layer(self, init_freqs_1d=[None, None],
                               sample_path=None):
        low_freqs_1d, high_freqs_1d = init_freqs_1d
        if init_freqs_1d[0] is None:
            low_freqs_1d = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        if init_freqs_1d[1] is None:
            high_freqs_1d = [0, 32, 64, 96, 128, 160, 192, 224, 256]
        self.hyper['init_freqs'] = [*low_freqs_1d, *high_freqs_1d]
        possible_frequencies = []
        for init_freqs_1d in [low_freqs_1d, high_freqs_1d]:
            freqs_1d = init_freqs_1d + [-i for i in init_freqs_1d]
            possible_frequencies.append(torch.tensor(list(product(freqs_1d,
                                                                  repeat=2))))
        possible_frequencies = torch.cat(possible_frequencies)

        chosen_frequencies = []
        for x, y in possible_frequencies:
            if x < 0 or (x == 0 and y <= 0):
                continue
            chosen_frequencies.append([x, y])
        chosen_frequencies = torch.tensor(
            chosen_frequencies
            ).reshape(-1, self.hyper['in_features'])

        # self.hyper['hidden_features'][0][0] = chosen_frequencies.shape[0] + 2
        # print(f'\nHidden matrix size: {self.hyper["hidden_features"][0][1]}x' +
            #   f'{self.hyper["hidden_features"][0][0]}\n')
        self.model = MRFactory.from_dict(self.hyper)

        # self.model.stages[0].first_layer.linear.weight = \
        #     torch.nn.Parameter(
        #     chosen_frequencies.float()[:self.hyper['hidden_features'][0][0] - 2],
        #     requires_grad=False
        # )
        nf = self.factor * torch.cat([
            chosen_frequencies.float()[:self.hyper['hidden_features'][0][0] - 2],
            torch.eye(self.hyper['in_features'])
            ], axis=0).reshape(-1, 2)
        self.model.stages[0].first_layer.linear.weight = torch.nn.Parameter(
            nf,
            requires_grad=False
        )
        self.input_freqs = 1 / self.factor * nf

    def _input_frequencies(self):
        self.model.frequencies = (
            1 / self.factor *
            self.model.stages[0].first_layer.linear.weight
        )

    def initialize_bias(self):
        for stage in range(self.model.n_stages()):
            stage_first_layer = self.model.stages[stage].first_layer.linear
            with torch.no_grad():
                torch.nn.init.uniform_(
                    stage_first_layer.bias,
                    -torch.pi/2,
                    torch.pi/2
                )

    def initialize_middle_layer(self):
        omega = self.model.stages[0].first_layer.linear.weight
        W = self.model.stages[-1].middle_layers[0].linear.weight

        chosen_frequencies = self.model.period / (2 * torch.pi) * omega
        device = 'cuda:0' if self.hyper['device'] == 'cuda' else 'cpu'
        clamps = create_clamps(self.hyper['bounds'],
        # clamps = create_clamps([1],
                               self.hyper['block_limits'],
                               chosen_frequencies,
                               device)

        new_W = dist.Normal(torch.zeros_like(clamps, device=device),
                            0.3 * clamps / self.hyper['hidden_omega_0']
                            ).sample([W.shape[0]])
        # new_W = clamps / hyper['hidden_omega_0'] * (2 * torch.rand_like(W,
        # device=device) - 1)
        self.model.stages[-1].middle_layers[0].linear.weight = \
            torch.nn.Parameter(new_W)

    def get_model(self):
        return self.model

    def initialize_middle_layer_with_identity(self, epsilon=1e-4):
        omega = self.model.stages[0].omega_G
        for i in range(0, len(self.model.stages[0].middle_layers)):
            W = torch.zeros_like(
                self.model.stages[0].middle_layers[i].linear.weight,
                requires_grad=False)
            min_dim = min(W.shape)
            W[:min_dim, :min_dim] = epsilon * torch.eye(min_dim) / (omega)
            with torch.no_grad():
                self.model.stages[0].middle_layers[i].linear.weight = \
                    torch.nn.Parameter(W, requires_grad=True)


class MRModule(nn.Module):
    """
    Built upon SIREN code
    """
    def __init__(self, in_features: int,
                 hidden_features: Union[int, Sequence],
                 hidden_layers: int,
                 out_features: int,
                 first_omega_0: int,
                 hidden_omega_0=1,
                 bias=False,
                 period=0,
                 prevknowledge=0,
                 **kwargs):
        super().__init__()

        self.bias = bias
        self.period = period

        if not isinstance(hidden_features, Sequence):
            hidden_features = [hidden_features] * (hidden_layers + 1)

        hidden_idx = 0
        self.first_layer = SineLayer(in_features, hidden_features[hidden_idx],
                                     bias=bias, is_first=True,
                                     omega_0=first_omega_0, period=period,
                                     **kwargs)

        middle = []
        bounds = kwargs.get('bounds', False)
        while hidden_idx < hidden_layers:
            middle.append(
                SineLayer(hidden_features[hidden_idx]
                          + (prevknowledge if hidden_idx == 0 else 0),
                          hidden_features[hidden_idx + 1], bias=True,
                          is_first=False, omega_0=hidden_omega_0,
                          bounds=bounds)
            )
            hidden_idx += 1
        # middle.append(
        #     SineLayer(prevknowledge + hidden_features[hidden_idx],
        #                hidden_features[hidden_idx + 1], bias=True,
        #                         is_first=False, omega_0=hidden_omega_0)
        # )
        # for i in range(hidden_layers - 1):
        #     middle.append(SineLayer(hidden_features, hidden_features,
        #                             bias=True, is_first=False,
        #                             omega_0=hidden_omega_0))

        self.middle_layers = nn.Sequential(*middle)

        self.final_linear = nn.Linear(hidden_features[hidden_idx],
                                      out_features)
        with torch.no_grad():
            self.final_linear.weight.uniform_(
                    -np.sqrt(6 / hidden_features[hidden_idx]) / hidden_omega_0,
                    np.sqrt(6 / hidden_features[hidden_idx]) / hidden_omega_0)

    # Check if internal layers initialization is needed/correct
    def reset_weights(self):
        def reset_sinelayer(m):
            if isinstance(m, SineLayer):
                m.init_weights()
        self.apply(reset_sinelayer)

    @property
    def in_features(self):
        return self.first_layer.in_features

    @property
    def out_features(self):
        return self.final_linear.out_features

    @property
    def hidden_features(self):
        # return self.first_layer.out_features
        hf = [self.middle_layers[0].linear.in_features]
        for layer in self.middle_layers:
            hf.append(layer.linear.out_features)
        return hf

    @property
    def hidden_layers(self):
        return len(self.middle_layers)

    @property
    def omega_0(self):
        return self.first_layer.omega_0

    @property
    def omega_G(self):
        return self.middle_layers[0].omega_0

    def forward(self, coords, prevbasis=None):
        proj = self.first_layer(coords)
        basis = (self.middle_layers(proj) if prevbasis is None
                 else self.middle_layers(torch.cat([proj, prevbasis], dim=-1)))
        out = self.final_linear(basis)
        return out, basis


class MRBoundedModule(MRModule):

    def forward(self, coords, prevbasis=None):
        proj = self.first_layer(coords)
        for i, layer in enumerate(self.middle_layers):
            boost = 30 / layer.omega_0
            layer.weight = nn.Parameter(
                layer.bound * boost *
                torch.tanh(layer.linear.weight * layer.omega_0))
            if prevbasis is not None and i == 0:
                proj = layer(torch.cat([proj, prevbasis], dim=-1))
            else:
                proj = layer(proj)
        out = self.final_linear(proj)
        return out, proj


class MRNet(nn.Module):
    """
    Build upon SIREN code
    """
    def __init__(self, in_features,
                 hidden_features,
                 hidden_layers,
                 out_features,
                 first_omega_0,
                 hidden_omega_0=1,
                 bias=False,
                 period=0,
                 superposition_w0=True,
                 **kwargs):
        super().__init__()

        self.superposition_w0 = superposition_w0
        self.in_features = in_features
        self.out_features = out_features
        self.bias = bias
        self.period = period
        bounds = kwargs.get('bounds', False)
        self.module = MRBoundedModule if bounds else MRModule
        first_module = self.module(in_features,
                                   hidden_features,
                                   hidden_layers,
                                   out_features,
                                   first_omega_0,
                                   hidden_omega_0,
                                   bias=bias,
                                   period=period,
                                   **kwargs)
        self.stages = nn.ModuleList([first_module])

    def init_lean_weights(self, mrmodule: MRModule):
        if self.period > 0:
            old_frequencies = []
            for stage in self.stages:
                device = self.current_device()
                last_stage_frequencies = stage.first_layer.linear.weight.cpu()
                old_frequencies.append(last_stage_frequencies.numpy())
                stage.first_layer.linear.weight.to(device)
            old_frequencies = np.concatenate(old_frequencies)

            mrmodule.first_layer.init_periodic_weights(
                tuple(map(tuple, (old_frequencies * self.period /
                                  (2 * torch.pi)).astype(np.int32)))
            )
        else:
            raise NotImplementedError("superposition_w0 'False' only" +
                                      "implemented for periodic signals")
            # w0 = mrmodule.first_layer.omega_0
            # prev_w0 = self.top_stage.first_layer.omega_0
            # layer_shape = mrmodule.first_layer.linear.weight.shape
            # hidden_feat, in_feat = layer_shape[0], layer_shape[1]
            # c = prev_w0/w0
            # p = torch.zeros(hidden_feat, in_feat).uniform_(-1, 1)
            # # transform the interval (0, 1] --> ( c, 1]
            # # and                    [-1,0] --> [-1,-c]
            # ca = (1-c)*p + c
            # cb = (1-c)*p - c
            # p = torch.where(p > 0, ca, cb)

            # with torch.no_grad():
            #     mrmodule.first_layer.linear.weight.copy_(p)

    def _add_stage(self, first_omega_0, hidden_features,
                   hidden_layers, hidden_omega_0, bias, prevknowledge):

        # TODO: Add learable bounds for multiresolution
        newstage = self.module(self.in_features,
                               hidden_features,
                               hidden_layers,
                               self.out_features,
                               first_omega_0,
                               hidden_omega_0,
                               bias=bias,
                               period=self.period,
                               prevknowledge=prevknowledge
                               ).to(self.current_device())
        if not self.superposition_w0:
            self.init_lean_weights(newstage)
        self.stages.append(newstage)

    def add_stage(self, first_omega_0, hidden_features,
                  hidden_layers, hidden_omega_0=1, bias=False):
        raise NotImplementedError

    def n_stages(self):
        return len(self.stages)

    @property
    def top_stage(self) -> MRModule:
        return self.stages[-1]

    def _aggregate_resolutions(self, mroutputs, mrweights, bias=False):
        device = self.current_device()
        if mrweights is None:
            mrweights = torch.ones(self.n_stages(), device=device)
        # Different weights per sample
        if len(mrweights.shape) == len(mroutputs[0].shape):
            concatenated = torch.concat(mroutputs, 1)
            weighted = torch.mul(concatenated, mrweights)
            return torch.sum(weighted, 1).unsqueeze(-1)
        # Same weights for all samples
        # aggr_layer = nn.Linear(self.n_stages(), self.out_features,
        #                         bias=bias, device=device)
        # for i in range(self.out_features):
        #     with torch.no_grad():
        #         aggr_layer.weight[i] = mrweights

        # aggregated = aggr_layer(torch.stack(mroutputs, dim=-1)).squeeze(-1)
        dims = [1] * len(mroutputs[0].shape)
        return (mrweights.view(self.n_stages(),
                               *dims) * torch.stack(mroutputs)).sum(dim=0)

    def parameters(self, recurse: bool = True) -> Iterator[Parameter]:
        if recurse:
            return super().parameters(recurse)

        return self.top_stage.parameters()

    def total_parameters(self):
        return sum(p.numel() for p in self.parameters()) - self.n_stages()

    def current_device(self) -> str:
        return next(self.parameters()).device

    def class_code(self):
        raise NotImplementedError


class MNet(MRNet):
    def init_from_dict(hyper):
        omega0, hidden_omega0 = hyper['omega_0'], hyper['hidden_omega_0']
        return MNet(
            hyper['in_features'],
            hyper['hidden_features'],
            hyper['hidden_layers'],
            hyper['out_features'],
            omega0[0] if isinstance(omega0, Sequence) else omega0,
            hidden_omega0[0] if isinstance(hidden_omega0, Sequence) else hidden_omega0,
            bias=hyper.get('bias', False),
            period=hyper.get('period', 0),
            superposition_w0=hyper['superposition_w0'],
        )

    def add_stage(self, first_omega_0, hidden_features,
                  hidden_layers, hidden_omega_0, bias):
        prev = self.top_stage.hidden_features[-1]
        return self._add_stage(first_omega_0, hidden_features, hidden_layers,
                               hidden_omega_0, bias, prev)

    def forward(self, coords, mrweights=None):
        # allows to take derivative w.r.t. input
        coords = coords.clone().detach().requires_grad_(True)
        mroutputs = []
        basis = None
        for mrstage in self.stages:
            out, basis = mrstage(coords, basis)
            mroutputs.append(out)
        y = self._aggregate_resolutions(mroutputs, mrweights)
        return {"model_in": coords, "model_out": y}

    def class_code(self):
        return 'M'


class LNet(MRNet):

    def init_from_dict(hyper):
        omega0, hidden_omega0 = hyper['omega_0'], hyper['hidden_omega_0']
        return LNet(
            hyper['in_features'],
            hyper['hidden_features'],
            hyper['hidden_layers'],
            hyper['out_features'],
            omega0[0] if isinstance(omega0, Sequence) else omega0,
            hidden_omega0[0] if isinstance(hidden_omega0,
                                           Sequence) else hidden_omega0,
            bias=hyper.get('bias', False),
            period=hyper.get('period', 0),
            superposition_w0=hyper['superposition_w0'],
        )

    def add_stage(self, first_omega_0, hidden_features,
                  hidden_layers, hidden_omega_0, bias):
        return self._add_stage(first_omega_0, hidden_features, hidden_layers,
                               hidden_omega_0, bias, 0)

    def forward(self, coords, mrweights=None):
        # allows to take derivative w.r.t. input
        coords = coords.clone().detach().requires_grad_(True)
        mroutputs = []
        for stage in self.stages:
            out, _ = stage(coords)
            mroutputs.append(out)

        # we could use another layer for a weighted sum
        y = self._aggregate_resolutions(mroutputs, mrweights)
        return {"model_in": coords, "model_out": y}

    def class_code(self):
        return 'L'


class SNet(MRNet):

    def init_from_dict(hyper):
        raise NotImplementedError

    def class_code(self):
        return 'S'


class MRFactory:

    def from_dict(hyper):
        omega0, hidden_omega0 = hyper['omega_0'], hyper['hidden_omega_0']
        if hyper['model'] == 'M':
            MRClass = MNet
        elif hyper['model'] == 'L':
            MRClass = LNet
        elif hyper['model'] == 'S':
            MRClass = SNet
        else:
            raise ValueError("model should be in ['M','L','M1']")

        hfeat, hlayers = hyper['hidden_features'], hyper['hidden_layers']
        # TODO: remove in future versions; for compatibility
        # only (periodic->period).
        period = 2 if hyper.get('periodic', False) else 0
        bandlimit = (omega0[0] if isinstance(omega0, Sequence) else omega0) / 2
        low_range = hyper.get('low_range', 10)
        perc_low_freqs = hyper.get('perc_low_freqs', 0.7)
        return MRClass(
            hyper['in_features'],
            hfeat[0] if isinstance(hfeat, Sequence) else hfeat,
            hlayers[0] if isinstance(hlayers, Sequence) else hlayers,
            hyper['out_features'],
            omega0[0] if isinstance(omega0, Sequence) else omega0,
            hidden_omega0[0] if isinstance(hidden_omega0, Sequence) else hidden_omega0,
            bias=hyper.get('bias', False),
            period=hyper.get('period', period),
            superposition_w0=hyper.get('superposition_w0', True),
            bandlimit=int(bandlimit),
            low_range=low_range,
            perc_low_freqs=perc_low_freqs,
            bounds=hyper.get('bounds', False),
            learn_bounds = hyper.get('learn_bounds', False),
        )

    def module_from_dict(hyper, idx=None):
        prevknowledge = 0
        if (idx > 0) and hyper['model'] in ['M']:
            prevknowledge = hyper['prevknowledge']
        # TODO: remove in future versions; for compatibility only
        # (periodic->period).
        period = 2 if hyper.get('periodic', False) else 0
        learn_bounds = hyper.get('learn_bounds', False)
        bounds = False
        if learn_bounds:
            bounds = hyper.get('bounds', False)
        module = MRBoundedModule if learn_bounds else MRModule

        return module(hyper['in_features'],
                      hyper['hidden_features'],
                      hyper['hidden_layers'],
                      hyper['out_features'],
                      hyper['omega_0'],
                      hyper['hidden_omega_0'],
                      hyper['bias'],
                      hyper.get('period', period),
                      prevknowledge,
                      bounds=bounds
                      )

    def save(model: MRNet, path: str):
        firstmodule = model.stages[0]

        omega_0 = [mod.omega_0 for mod in model.stages]
        hidden_omega_0 = [mod.omega_G for mod in model.stages]
        hidden_layers = [mod.hidden_layers for mod in model.stages]
        hidden_features = [mod.hidden_features for mod in model.stages]
        bias = [mod.bias for mod in model.stages]
        mdict = {
                'omega_0': omega_0,
                'hidden_omega_0': hidden_omega_0,
                'model': model.class_code(),
                'stages': model.n_stages(),
                'in_features': firstmodule.in_features,
                'out_features': firstmodule.out_features,
                'hidden_layers': hidden_layers,
                'hidden_features': hidden_features,
                'bias': bias,
                'period': model.period,
                'superposition_w0': model.superposition_w0
            }
        for stg in range(model.n_stages()):
            mdict[f'module{stg}_state_dict'] = model.stages[stg].state_dict()
        torch.save(mdict, path)

    def load_state_dict(filepath):
        checkpoint = torch.load(filepath, map_location=torch.device('cpu'))
        singledict = deepcopy(checkpoint)
        module_keys = ['omega_0', 'hidden_omega_0', 'hidden_features',
                       'hidden_layers', 'bias']
        updict = {k: checkpoint[k][0] for k in module_keys}
        singledict.update(updict)
        model = MRFactory.from_dict(singledict)
        # print(model)
        model_stages = []
        singledict['prevknowledge'] = 0
        for stage in range(checkpoint['stages']):
            updict = {k: checkpoint[k][stage] for k in module_keys}
            # GAMBIARRA
            updict['hidden_features'][0] -= singledict['prevknowledge']
            singledict.update(updict)
            mrmodule = MRFactory.module_from_dict(singledict, stage)
            mrmodule.load_state_dict(
                checkpoint[f'module{stage}_state_dict'])
            model_stages.append(mrmodule)
            singledict['prevknowledge'] = mrmodule.hidden_features[-1]

        model.stages = nn.ModuleList(model_stages)
        model.eval()
        return model
