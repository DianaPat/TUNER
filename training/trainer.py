import torch
import torch.nn.functional as F

from enum import Enum, auto
from torch.utils.data import DataLoader
from typing import Sequence, Union

from mrnet.networks.mrnet import MRNet
import mrnet.training.loss as mrloss
from mrnet.training.listener import TrainingListener
from mrnet.training.optimizer import OptimizationHandler


class MRMode(Enum):
    FILTERING = auto()
    CAPACITY = auto()

class MRTrainType(Enum):
    STACK = auto()
    STAGES = auto()

BIG_VALUE = 10000000

class MRTrainer:
    def __init__(self, model: MRNet, 
                        datasource: Union[DataLoader, Sequence[DataLoader]], 
                        testsource: Union[DataLoader, Sequence[DataLoader]],
                        listener: TrainingListener,
                        omega0: Union[int, Sequence[int]],
                        hidden_omega0: Union[int, Sequence[int]],
                        hidden_features: Union[int, Sequence[int]],
                        hidden_layers: Union[int, Sequence[int]],
                        max_stages: int,
                        max_epochs_per_stage: Union[int, Sequence[int]],
                        loss_tol: Union[float, Sequence[float]],
                        diff_tol: Union[float, Sequence[float]],
                        learning_rate: Union[float, Sequence[float]],
                        opt_method=torch.optim.Adam,
                        loss_function=F.mse_loss,
                        loss_weights={'d0': 1.0},
                        handler_class=OptimizationHandler,
                        bias=False,
                        mr_train_type=MRTrainType):
        
        if model.n_stages() > 1:
            raise ValueError("Must initialize with untrained model")

        # TODO: It's broken in current architecture; discuss removal of DataLoader from pipeline; sampler could be inside dataloader
        if isinstance(datasource, DataLoader):      
            self.mode = MRMode.CAPACITY
        else:
            self.mode = MRMode.FILTERING

        if self.mode == MRMode.FILTERING and len(datasource) != max_stages:
            raise ValueError("'max_stages' must match 'datasource' length for FILTERING based training")
        if self.mode == MRMode.FILTERING and len(datasource) != len(testsource):
            raise ValueError("Number of Train and Test signals must match ")
        
        self.train_type = mr_train_type

        self.model = model
        self.max_stages = max_stages
        self.listener = listener

        self._datasource = datasource
        self._testsource = testsource
        self._loss_tol = loss_tol
        self._diff_tol = diff_tol
        self._loss_weights = loss_weights
        self._max_epochs_per_stage = max_epochs_per_stage
        self._hidden_features = hidden_features
        self._hidden_layers = hidden_layers
        
        self._learning_rate = learning_rate
        self.hidden_omega0 = hidden_omega0

        self.bias=bias

        if isinstance(omega0, Sequence):
            self.omega0 = omega0
        else:
            self.omega0 = [k * omega0 for k in range(1, max_stages + 1)]
        
        self.opt_method = opt_method
        self.loss_function = loss_function
        self._handler_class = handler_class

        # training parameters to be exposed
        self.current_loss = None
        self.stages_losses = []
        self.n_stages = 1

        self.epochs_per_stage = []
        self._total_epochs_trained = 0


    def init_from_dict(model: MRNet, 
                        datasource: Union[DataLoader, Sequence[DataLoader]], 
                        testsource: Union[DataLoader, Sequence[DataLoader]],
                        listener: TrainingListener,
                        hyper: dict,
                        **kwargs):
        lr = hyper.get('lr', 1e-4)
        # TODO: think of strategy to use string or objects for these hyperparameters:

        if hyper.get('filter', None) == 'laplace':
            mr_train_type = MRTrainType.STAGES
        else:
            mr_train_type = MRTrainType.STACK
        
        try:
            loss_func = kwargs['loss_function']
        except KeyError:
            loss_func = mrloss.get_loss_from_map(hyper['loss_function'])

        try:
            handler_class = kwargs['optim_handler']
        except KeyError:
            handler_class = OptimizationHandler

        return MRTrainer(model,
                        datasource,
                        testsource,
                        listener,
                        hyper['omega_0'],
                        hyper['hidden_omega_0'],
                        hyper['hidden_features'],
                        hyper['hidden_layers'],
                        hyper['max_stages'],
                        hyper['max_epochs_per_stage'],
                        hyper.get('loss_tol', 1e-11), 
                        diff_tol=hyper.get('diff_tol', 1e-7),
                        learning_rate=lr,
                        loss_function=loss_func,
                        loss_weights=hyper['loss_weights'],
                        handler_class=handler_class,
                        bias=hyper.get('bias', False),
                        mr_train_type=mr_train_type)

    @property
    def current_datasource(self)-> DataLoader:
        if self.mode == MRMode.CAPACITY:
            return self._datasource
        return self._datasource[-self.n_stages]

    @property
    def current_testloader(self)-> DataLoader:
        if self.mode == MRMode.CAPACITY:
            return self._testsource
        return self._testsource[-self.n_stages]

    @property
    def current_loss_tol(self) -> float:
        if isinstance(self._loss_tol, Sequence):
            return self._loss_tol[self.n_stages - 1]
        return self._loss_tol

    @property
    def current_diff_tol(self) -> float:
        if isinstance(self._diff_tol, Sequence):
            return self._diff_tol[self.n_stages - 1]
        return self._diff_tol

    @property
    def current_limit_for_epochs(self) -> int:
        if isinstance(self._max_epochs_per_stage, Sequence):
            return self._max_epochs_per_stage[self.n_stages - 1]
        return self._max_epochs_per_stage

    @property
    def current_learning_rate(self) -> float:
        if isinstance(self._learning_rate, Sequence):
            return self._learning_rate[self.n_stages - 1]
        return self._learning_rate
    
    def _get_hidden_features(self, next:bool) -> int:
        if isinstance(self._hidden_features, Sequence):
            idx = self.n_stages if next else (self.n_stages -1)
            return self._hidden_features[idx]
        return self._hidden_features
    
    @property
    def current_hidden_features(self) -> int:
        return self._get_hidden_features(False)

    def next_hidden_features(self) -> int:
        return self._get_hidden_features(True)

    def _get_hidden_layers(self, next:bool) -> int:
        if isinstance(self._hidden_layers, Sequence):
            idx = self.n_stages if next else (self.n_stages -1)
            return self._hidden_layers[idx]
        return self._hidden_layers

    @property
    def current_hidden_layers(self):
        return self._get_hidden_layers(False)

    def next_hidden_layers(self):
        return self._get_hidden_layers(True)

    def _get_bias(self):
        return self.bias
    
    def _get_omega0(self, next=False):
        idx = self.n_stages if next else (self.n_stages - 1)
        return self.omega0[idx]

    @property
    def current_omega0(self):
        return self._get_omega0(False)

    def next_omega0(self):
        return self._get_omega0(True)

    def _get_hidden_omega0(self, next=False):
        if isinstance(self.hidden_omega0, Sequence):
            return self.hidden_omega0[self.n_stages - (0 if next else 1)]
        return self.hidden_omega0

    @property
    def current_hidden_omega0(self):
        return self._get_hidden_omega0(False)

    def next_hidden_omega0(self):
        return self._get_hidden_omega0(True)

    def total_epochs_trained(self):
        return self._total_epochs_trained

    def trained_epochs_per_stage(self):
        return self.epochs_per_stage

    def get_stage_hyper(self):
        return {
                'omega_0': self.current_omega0,
                'hidden_omega_0': self.current_hidden_omega0,
                'hidden_features': self.current_hidden_features,
                'hidden_layers': self.current_hidden_layers,
                'max_epochs_per_stage': self.current_limit_for_epochs,
                'loss_tol': self.current_loss_tol,
                'diff_tol': self.current_diff_tol,
                'stage': self.n_stages
            }

    def bounds_to_device(self, device):
        device = torch.device('cuda:0' if device == 'cuda' else 'cpu')
        if hasattr(self.model.stages[0].first_layer, 'bounds'):
            self.model.stages[0].first_layer.bounds = \
                self.model.stages[0].first_layer.bounds.to(device)
        for layer in self.model.stages[0].middle_layers:
            if hasattr(layer, 'bounds'):
                layer.bounds = layer.bounds.to(device)

    def train(self, device='cpu'):
        self.model.to(device)
        self.bounds_to_device(device)
        self.model.train()

        self.listener.on_train_start()
        initial_stage = self.model.n_stages()
        for stage in range(initial_stage, self.max_stages + 1):
            if stage > initial_stage:
                self.model.add_stage(
                    self.next_omega0(),
                    self.next_hidden_features(),
                    self.next_hidden_layers(),
                    self.next_hidden_omega0(),
                    self._get_bias(),
                )
                self.model.to(device)

            self.n_stages = stage
            optimizer = self.opt_method(lr=self.current_learning_rate, 
                                params=self.model.parameters(recurse=False))
            optim_handler = self._handler_class(self.model, 
                                                optimizer, 
                                                self.loss_function,
                                                self._loss_weights)
            
            tolerance_reached = False
            self.listener.on_stage_start(self.get_model(),
                                        self.n_stages, 
                                        self.get_stage_hyper())
            last_epoch_loss = BIG_VALUE
            print("DATA SIZE", self.current_datasource.size())
            for epoch in range(self.current_limit_for_epochs):
                for batch in self.current_datasource:
                    running_loss = optim_handler(batch, device)
                    self.listener.on_batch_finish(running_loss)

                epoch_loss = {key: value / len(self.current_datasource)
                                for key, value in running_loss.items() }
                total_epoch_loss = sum(epoch_loss.values())
                self._total_epochs_trained += 1
                loss_gain = abs((total_epoch_loss - last_epoch_loss)
                                / total_epoch_loss)
                if ((total_epoch_loss < self.current_loss_tol) 
                or (loss_gain < self.current_diff_tol)
                or (epoch == self.current_limit_for_epochs - 1)):
                    self.epochs_per_stage.append(epoch + 1)
                    tolerance_reached = True
                
                last_epoch_loss = total_epoch_loss
                self.listener.on_epoch_finish(self.get_model(), epoch_loss)
                if tolerance_reached:
                    break
            optim_handler = None
            self.listener.on_stage_trained(self.get_model(), 
                                        self.current_datasource,
                                        self.current_testloader)
        
        self.listener.on_train_finish(self.get_model(), 
                                    self._total_epochs_trained)


    def get_model(self)-> MRNet:
        return self.model
