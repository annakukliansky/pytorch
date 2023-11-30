# Owner(s): ["oncall: mobile"]
import copy
import operator

import torch
import torch._dynamo as torchdynamo
from torch._export import capture_pre_autograd_graph
from torch.ao.ns.fx.utils import compute_sqnr
from torch.ao.quantization import (
    default_dynamic_fake_quant,
    observer,
    QConfig,
    QConfigMapping,
)
from torch.ao.quantization.backend_config import get_qnnpack_backend_config
from torch.ao.quantization.qconfig import (
    default_per_channel_symmetric_qnnpack_qconfig,
    default_symmetric_qnnpack_qconfig,
    per_channel_weight_observer_range_neg_127_to_127,
    weight_observer_range_neg_127_to_127,
)
from torch.ao.quantization.quantize_fx import (
    _convert_to_reference_decomposed_fx,
    convert_to_reference_fx,
    prepare_fx,
)
from torch.ao.quantization.quantize_pt2e import convert_pt2e, prepare_pt2e
from torch.ao.quantization.quantizer.xnnpack_quantizer import (
    get_symmetric_quantization_config,
    XNNPACKQuantizer,
)
from torch.testing._internal.common_quantization import (
    NodeSpec as ns,
    skip_if_no_torchvision,
    skipIfNoQNNPACK,
    TestHelperModules,
)
from torch.testing._internal.common_quantized import override_quantized_engine

from .test_quantize_pt2e import PT2EQuantizationTestCase


@skipIfNoQNNPACK
class TestXNNPACKQuantizer(PT2EQuantizationTestCase):
    def test_conv1d(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        example_inputs = (torch.randn(1, 3, 5),)
        node_occurrence = {
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 2,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 2,
            torch.ops.quantized_decomposed.quantize_per_channel.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_channel.default: 1,
        }
        node_list = [
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.conv1d.default,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
        ]
        self._test_quantizer(
            TestHelperModules.ConvWithBNRelu(dim=1, relu=False, bn=False),
            example_inputs,
            quantizer,
            node_occurrence,
            node_list,
        )

    def test_conv2d(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        example_inputs = (torch.randn(1, 3, 5, 5),)
        node_occurrence = {
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 2,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 2,
            # quantize_per_channel for weights are const propagated
            torch.ops.quantized_decomposed.quantize_per_channel.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_channel.default: 1,
        }
        node_list = [
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.conv2d.default,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
        ]
        self._test_quantizer(
            TestHelperModules.ConvWithBNRelu(relu=False, bn=False),
            example_inputs,
            quantizer,
            node_occurrence,
            node_list,
        )

    def test_conv1d_with_conv2d(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        example_inputs = (torch.randn(1, 3, 5, 5),)
        node_occurrence = {
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 4,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 4,
            torch.ops.quantized_decomposed.quantize_per_channel.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_channel.default: 2,
        }
        node_list = [
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.conv2d.default,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.conv1d.default,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
        ]
        self._test_quantizer(
            TestHelperModules.Conv1dWithConv2d(),
            example_inputs,
            quantizer,
            node_occurrence,
            node_list,
        )

    def test_linear(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        m_eager = TestHelperModules.TwoLinearModule().eval()

        # Test with 2d inputs
        example_inputs_2d = (torch.randn(9, 8),)
        example_inputs_3d = (torch.randn(9, 10, 8),)
        example_inputs_4d = (torch.randn(9, 10, 11, 8),)
        node_occurrence = {
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 3,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 3,
            # quantize_per_channel for weights are const propagated
            torch.ops.quantized_decomposed.quantize_per_channel.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_channel.default: 2,
        }
        qconfig = default_per_channel_symmetric_qnnpack_qconfig
        qconfig_mapping = QConfigMapping().set_global(qconfig)
        for example_inputs in [example_inputs_2d, example_inputs_3d, example_inputs_4d]:
            self._test_quantizer(
                m_eager,
                example_inputs,
                quantizer,
                node_occurrence,
                [],
                True,
                qconfig_mapping,
            )

    def test_conv_linear_no_permute(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        node_occurrence = {
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 5,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 5,
            # quantize_per_channel for weights are const propagated
            torch.ops.quantized_decomposed.quantize_per_channel.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_channel.default: 3,
        }
        qconfig = default_per_channel_symmetric_qnnpack_qconfig
        qconfig_mapping = QConfigMapping().set_global(qconfig)
        # Test with 2d inputs
        example_inputs = (torch.randn(2, 3, 4, 4),)
        self._test_quantizer(
            TestHelperModules.Conv2dWithTwoLinear(),
            example_inputs,
            quantizer,
            node_occurrence,
            [],
            True,
            qconfig_mapping,
        )

    def test_conv_linear(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)

        # Test with 2d inputs
        example_inputs = (torch.randn(2, 3, 4, 4),)
        node_occurrence = {
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 5,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 5,
            # quantize_per_channel for weights are const propagated
            torch.ops.quantized_decomposed.quantize_per_channel.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_channel.default: 3,
        }
        qconfig = default_per_channel_symmetric_qnnpack_qconfig
        qconfig_mapping = QConfigMapping().set_global(qconfig)
        self._test_quantizer(
            TestHelperModules.Conv2dWithTwoLinearPermute(),
            example_inputs,
            quantizer,
            node_occurrence,
            [],
            True,
            qconfig_mapping,
        )

    def test_linear_with_dynamic_shape(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        m_eager = TestHelperModules.TwoLinearModule().eval()

        # Test with 2d inputs
        example_inputs_3d = (torch.randn(9, 10, 8),)
        node_occurrence = {
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 3,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 3,
            # quantize_per_channel for weights are const propagated
            torch.ops.quantized_decomposed.quantize_per_channel.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_channel.default: 2,
        }
        qconfig = default_per_channel_symmetric_qnnpack_qconfig
        qconfig_mapping = QConfigMapping().set_global(qconfig)
        self._test_quantizer(
            m_eager,
            example_inputs_3d,
            quantizer,
            node_occurrence,
            [],
            True,
            qconfig_mapping,
            export_with_dynamic_shape=True,
        )

    def test_obs_sharing_ops(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        m = TestHelperModules.Conv2dWithObsSharingOps().eval()
        example_inputs = (torch.randn(1, 3, 5, 5),)
        node_occurrence = {
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 5,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 5,
            # quantize_per_channel for weights are const propagated
            torch.ops.quantized_decomposed.quantize_per_channel.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_channel.default: 1,
        }
        node_list = [
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.conv2d.default,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.adaptive_avg_pool2d.default,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.hardtanh.default,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.mean.default,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
        ]
        self._test_quantizer(m, example_inputs, quantizer, node_occurrence, node_list)

    def test_set_module_name(self):
        class Sub(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(5, 5)

            def forward(self, x):
                return self.linear(x)

        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(5, 5)
                self.sub = Sub()

            def forward(self, x):
                x = self.linear(x)
                x = self.sub(x)
                return x

        m = M().eval()
        example_inputs = (torch.randn(3, 5),)
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_module_name("sub", quantization_config)
        node_occurrence = {
            torch.ops.aten.linear.default: 2,
            # input and output for the second linear
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 2,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 2,
        }
        node_list = [
            # first linear is not quantized
            torch.ops.aten.linear.default,
            # second linear is quantized
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.linear.default,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
        ]
        self._test_quantizer(m, example_inputs, quantizer, node_occurrence, node_list)

    def test_set_module_type(self):
        class Sub(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(5, 5)

            def forward(self, x):
                return self.linear(x)

        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(5, 5)
                self.sub = Sub()

            def forward(self, x):
                x = self.linear(x)
                x = self.sub(x)
                return x

        m = M().eval()
        example_inputs = (torch.randn(3, 5),)
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_module_type(Sub, quantization_config)
        node_occurrence = {
            torch.ops.aten.linear.default: 2,
            # input and output for the second linear
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 2,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 2,
        }
        node_list = [
            # first linear is not quantized
            torch.ops.aten.linear.default,
            # second linear is quantized
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.linear.default,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
        ]
        self._test_quantizer(m, example_inputs, quantizer, node_occurrence, node_list)

    def test_propagate_annotation(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        m = TestHelperModules.Conv2dPropAnnotaton().eval()
        example_inputs = (torch.randn(1, 3, 5, 5),)

        # program capture
        m = capture_pre_autograd_graph(
            m,
            example_inputs,
        )

        m = prepare_pt2e(m, quantizer)
        m(*example_inputs)
        act_post_processes_pairs = []
        for n in m.graph.nodes:
            if n.target in [
                torch.ops.aten.view.default,
                torch.ops.aten.hardtanh.default,
            ]:
                input_act = getattr(m, n.args[0].target)
                output_act = getattr(m, list(n.users)[0].target)
                self.assertIs(input_act, output_act)

        m = convert_pt2e(m, fold_quantize=True)
        node_occurrence = {
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            ns.call_function(
                torch.ops.quantized_decomposed.quantize_per_tensor.default
            ): 5,
            ns.call_function(
                torch.ops.quantized_decomposed.dequantize_per_tensor.default
            ): 5,
            # note: quantize op for weights are const propagated
            ns.call_function(
                torch.ops.quantized_decomposed.quantize_per_channel.default
            ): 0,
            ns.call_function(
                torch.ops.quantized_decomposed.dequantize_per_channel.default
            ): 2,
        }
        self.checkGraphModuleNodes(m, expected_node_occurrence=node_occurrence)

    def test_dynamic_linear(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(
            is_per_channel=True, is_dynamic=True
        )
        quantizer.set_global(quantization_config)
        m_eager = TestHelperModules.TwoLinearModule().eval()

        node_occurrence = {
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            torch.ops.quantized_decomposed.quantize_per_tensor.tensor: 2,
            torch.ops.quantized_decomposed.dequantize_per_tensor.tensor: 2,
            # note: quantize op for weights are const propagated
            torch.ops.quantized_decomposed.quantize_per_channel.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_channel.default: 2,
        }
        act_affine_quant_obs = observer.PlaceholderObserver.with_args(
            dtype=torch.qint8,
            qscheme=torch.per_tensor_affine,
            quant_min=-128,
            quant_max=127,
            eps=2**-12,
            is_dynamic=True,
        )
        qconfig = QConfig(
            activation=act_affine_quant_obs,
            weight=per_channel_weight_observer_range_neg_127_to_127,
        )
        qconfig_mapping = QConfigMapping().set_global(qconfig)
        # Test with 2d inputs
        example_inputs_2d = (torch.randn(9, 8),)
        example_inputs_4d = (torch.randn(9, 10, 11, 8),)
        for example_inputs in [example_inputs_2d, example_inputs_4d]:
            self._test_quantizer(
                m_eager,
                example_inputs,
                quantizer,
                node_occurrence,
                [],
                True,
                qconfig_mapping,
            )

    def test_qat_dynamic_linear(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(
            is_per_channel=True,
            is_dynamic=True,
            is_qat=True,
        )
        quantizer.set_global(quantization_config)
        m_eager = TestHelperModules.TwoLinearModule().eval()

        node_occurrence = {
            torch.ops.quantized_decomposed.choose_qparams.tensor: 2,
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            torch.ops.quantized_decomposed.quantize_per_tensor.tensor: 2,
            torch.ops.quantized_decomposed.dequantize_per_tensor.tensor: 2,
            # note: quantize op for weights are const propagated
            torch.ops.quantized_decomposed.quantize_per_channel.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_channel.default: 2,
        }
        act_affine_quant_obs = default_dynamic_fake_quant
        qconfig = QConfig(
            activation=act_affine_quant_obs,
            weight=per_channel_weight_observer_range_neg_127_to_127,
        )
        qconfig_mapping = QConfigMapping().set_global(qconfig)
        # Test with 2d inputs
        example_inputs_2d = (torch.randn(9, 8),)
        example_inputs_4d = (torch.randn(9, 10, 11, 8),)
        for example_inputs in [example_inputs_2d, example_inputs_4d]:
            self._test_quantizer(
                m_eager,
                example_inputs,
                quantizer,
                node_occurrence,
                [],
                True,
                qconfig_mapping,
                is_qat=True,
            )

    def test_dynamic_linear_with_conv(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(
            is_per_channel=False, is_dynamic=True
        )
        quantizer.set_global(quantization_config)
        m_eager = TestHelperModules.ConvLinearWPermute().eval()

        node_occurrence = {
            # input and output are using quantize_per_tensor and weight is using quantize_per_channel
            torch.ops.quantized_decomposed.quantize_per_tensor.tensor: 1,
            torch.ops.quantized_decomposed.dequantize_per_tensor.tensor: 1,
            # note: quantize op for weights are const propagated
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 0,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 1,
        }
        act_affine_quant_obs = observer.PlaceholderObserver.with_args(
            dtype=torch.qint8,
            qscheme=torch.per_tensor_affine,
            quant_min=-128,
            quant_max=127,
            eps=2**-12,
            is_dynamic=True,
        )
        qconfig = QConfig(
            activation=act_affine_quant_obs,
            weight=weight_observer_range_neg_127_to_127,
        )
        # Test with 2d inputs
        example_inputs = (torch.randn(2, 3, 4, 4),)
        qconfig_mapping = QConfigMapping().set_global(qconfig)
        self._test_quantizer(
            m_eager,
            example_inputs,
            quantizer,
            node_occurrence,
            [],
            True,
            qconfig_mapping,
        )

    def test_gru(self):
        """this is a test for annotating fp32 GRU so that it produces
        q -> dq -> fp32_gru -> q -> dq, this is currently enough for our use cases,
        but we may change the annotation to be more precise in the future
        """

        class RNNDynamicModel(torch.nn.Module):
            def __init__(self, mod_type):
                super().__init__()
                self.qconfig = default_dynamic_qconfig
                if mod_type == "GRU":
                    self.mod = torch.nn.GRU(2, 2).to(dtype=torch.float)
                if mod_type == "LSTM":
                    self.mod = torch.nn.LSTM(2, 2).to(dtype=torch.float)

            def forward(self, input_tensor, hidden_tensor):
                input_tensor = 1 * input_tensor
                hidden_tensor = 1 * hidden_tensor
                output_tensor, hidden_out = self.mod(input_tensor, hidden_tensor)
                return 1 * output_tensor, 1 * hidden_out

        with override_quantized_engine("qnnpack"):
            model_fx = RNNDynamicModel("GRU")
            module_types = [torch.nn.GRU]
            niter = 10
            example_inputs = (
                # input_tensor
                torch.tensor([[100, -155], [-155, 100], [100, -155]], dtype=torch.float)
                .unsqueeze(0)
                .repeat(niter, 1, 1),
                # hidden_tensor
                # (D * num_layers, N, H_out)
                torch.tensor([[[100, -155]]], dtype=torch.float).repeat(1, 3, 1),
            )
            model_graph = copy.deepcopy(model_fx)

            qconfig_mapping = QConfigMapping().set_object_type(
                operator.mul, default_symmetric_qnnpack_qconfig
            )
            model_fx = prepare_fx(
                model_fx,
                qconfig_mapping,
                example_inputs,
                backend_config=get_qnnpack_backend_config(),
            )
            model_fx(*example_inputs)
            model_fx = _convert_to_reference_decomposed_fx(model_fx)

            with torchdynamo.config.patch(allow_rnn=True):
                model_graph = capture_pre_autograd_graph(
                    model_graph,
                    example_inputs,
                )
            quantizer = XNNPACKQuantizer()
            quantization_config = get_symmetric_quantization_config(
                is_per_channel=False, is_dynamic=False
            )
            quantizer.set_global(quantization_config)
            model_graph = prepare_pt2e(model_graph, quantizer)
            model_graph(*example_inputs)
            model_graph = convert_pt2e(model_graph, fold_quantize=True)
            self.assertEqual(model_fx(*example_inputs), model_graph(*example_inputs))

    def test_linear_gru(self):
        """this test is to make sure GRU annotation does not interfere with linear annotation"""

        class RNNDynamicModel(torch.nn.Module):
            def __init__(self, mod_type):
                super().__init__()
                self.qconfig = default_dynamic_qconfig
                self.linear = torch.nn.Linear(2, 2)
                if mod_type == "GRU":
                    self.mod = torch.nn.GRU(2, 2).to(dtype=torch.float)
                if mod_type == "LSTM":
                    self.mod = torch.nn.LSTM(2, 2).to(dtype=torch.float)

            def forward(self, input_tensor, hidden_tensor):
                input_tensor = self.linear(input_tensor)
                input_tensor = 1 * input_tensor
                hidden_tensor = 1 * hidden_tensor
                output_tensor, hidden_out = self.mod(input_tensor, hidden_tensor)
                return 1 * output_tensor, 1 * hidden_out

        with override_quantized_engine("qnnpack"):
            model_fx = RNNDynamicModel("GRU")
            module_types = [torch.nn.GRU]
            niter = 10
            example_inputs = (
                # input_tensor
                torch.tensor([[100, -155], [-155, 100], [100, -155]], dtype=torch.float)
                .unsqueeze(0)
                .repeat(niter, 1, 1),
                # hidden_tensor
                # (D * num_layers, N, H_out)
                torch.tensor([[[100, -155]]], dtype=torch.float).repeat(1, 3, 1),
            )
            model_graph = copy.deepcopy(model_fx)

            qconfig_mapping = (
                QConfigMapping()
                .set_object_type(operator.mul, default_symmetric_qnnpack_qconfig)
                .set_object_type(torch.nn.Linear, default_symmetric_qnnpack_qconfig)
            )
            model_fx = prepare_fx(
                model_fx,
                qconfig_mapping,
                example_inputs,
                backend_config=get_qnnpack_backend_config(),
            )
            model_fx(*example_inputs)
            model_fx = _convert_to_reference_decomposed_fx(model_fx)

            with torchdynamo.config.patch(allow_rnn=True):
                model_graph = capture_pre_autograd_graph(
                    model_graph,
                    example_inputs,
                )
            quantizer = XNNPACKQuantizer()
            quantization_config = get_symmetric_quantization_config(
                is_per_channel=False, is_dynamic=False
            )
            quantizer.set_global(quantization_config)
            model_graph = prepare_pt2e(model_graph, quantizer)
            model_graph(*example_inputs)
            model_graph = convert_pt2e(model_graph, fold_quantize=True)
            self.assertEqual(model_fx(*example_inputs), model_graph(*example_inputs))

    def test_add_and_inplace_add(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        example_inputs = (
            torch.randn(1, 3, 5, 5),
            torch.randn(1, 3, 5, 5),
        )
        node_occurrence = {
            # two input and one output for first add, and output for second add
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 4,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 5,
        }
        node_list = [
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.add.Tensor,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.add_.Tensor,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
        ]
        self._test_quantizer(
            TestHelperModules.AddInplaceAdd(),
            example_inputs,
            quantizer,
            node_occurrence,
            node_list,
        )

    def test_mul_and_inplace_mul(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        example_inputs = (
            torch.randn(1, 3, 5, 5),
            torch.randn(1, 3, 5, 5),
        )
        node_occurrence = {
            # two input and one output for first add, and output for second add
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 4,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 5,
        }
        node_list = [
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.mul.Tensor,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.mul_.Tensor,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
        ]
        self._test_quantizer(
            TestHelperModules.MulInplaceMul(),
            example_inputs,
            quantizer,
            node_occurrence,
            node_list,
        )

    def test_add_mul_scalar(self):
        quantizer = XNNPACKQuantizer()
        quantization_config = get_symmetric_quantization_config(is_per_channel=True)
        quantizer.set_global(quantization_config)
        example_inputs = (torch.randn(1, 3, 5, 5),)
        node_occurrence = {
            # two input and one output for first add, and output for second add
            torch.ops.quantized_decomposed.quantize_per_tensor.default: 5,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default: 7,
        }
        node_list = [
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.add.Tensor,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.mul.Tensor,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.add_.Tensor,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
            torch.ops.aten.mul_.Tensor,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
        ]
        self._test_quantizer(
            TestHelperModules.AddMulScalar(),
            example_inputs,
            quantizer,
            node_occurrence,
            node_list,
        )


# TODO: express this using self._test_quantizer, add test for inception_v4
class TestXNNPACKQuantizerModels(PT2EQuantizationTestCase):
    @skip_if_no_torchvision
    @skipIfNoQNNPACK
    def test_resnet18(self):
        import torchvision

        with override_quantized_engine("qnnpack"):
            example_inputs = (torch.randn(1, 3, 224, 224),)
            m = torchvision.models.resnet18().eval()
            m_copy = copy.deepcopy(m)
            # program capture
            m = capture_pre_autograd_graph(
                m,
                example_inputs,
            )

            quantizer = XNNPACKQuantizer()
            quantization_config = get_symmetric_quantization_config(is_per_channel=True)
            quantizer.set_global(quantization_config)
            m = prepare_pt2e(m, quantizer)
            # checking that we inserted observers correctly for maxpool operator (input and
            # output share observer instance)
            self.assertEqual(
                id(m.activation_post_process_3), id(m.activation_post_process_2)
            )
            after_prepare_result = m(*example_inputs)
            m = convert_pt2e(m, fold_quantize=True)

            after_quant_result = m(*example_inputs)

            # comparing with existing fx graph mode quantization reference flow
            qconfig = default_per_channel_symmetric_qnnpack_qconfig
            qconfig_mapping = QConfigMapping().set_global(qconfig)
            backend_config = get_qnnpack_backend_config()
            m_fx = prepare_fx(
                m_copy, qconfig_mapping, example_inputs, backend_config=backend_config
            )
            after_prepare_result_fx = m_fx(*example_inputs)
            m_fx = convert_to_reference_fx(m_fx, backend_config=backend_config)

            after_quant_result_fx = m_fx(*example_inputs)

            # the result matches exactly after prepare
            # Note: this currently will always be true since we are inserting observers
            # the check becomes useful when we add qat examples
            # but we can still manully inspect the printed observers to make sure
            # it matches
            self.assertEqual(after_prepare_result, after_prepare_result_fx)
            self.assertEqual(
                compute_sqnr(after_prepare_result, after_prepare_result_fx),
                torch.tensor(float("inf")),
            )
            # there are slight differences after convert due to different implementations
            # of quant/dequant
            self.assertTrue(
                torch.max(after_quant_result - after_quant_result_fx) < 1e-1
            )
            self.assertTrue(
                compute_sqnr(after_quant_result, after_quant_result_fx) > 35
            )
