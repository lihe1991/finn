# Copyright (c) 2020, Xilinx
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of FINN nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import os
import numpy as np

try:
    from pyverilator import PyVerilator
except ModuleNotFoundError:
    PyVerilator = None
from finn.custom_op.fpgadataflow import HLSCustomOp
from finn.core.datatype import DataType
from onnx import TensorProto, helper
from finn.util.basic import roundup_to_integer_multiple
from finn.util.data_packing import npy_to_rtlsim_input, rtlsim_output_to_npy

# does not do anything at the ONNX node-by-node level, and input-output
# tensor shapes are the same. performs data width conversion at the rtlsim level


class StreamingDataWidthConverter_Batch(HLSCustomOp):
    """Class that corresponds to finn-hlslib StreamingDataWidthConverter_Batch
    function."""

    def get_nodeattr_types(self):
        my_attrs = {
            # shape of input/output tensors
            "shape": ("ints", True, []),
            # bit width of input and output streams
            "inWidth": ("i", True, 0),
            "outWidth": ("i", True, 0),
            # FINN DataTypes for inputs/outputs
            "dataType": ("s", True, ""),
        }
        my_attrs.update(super().get_nodeattr_types())
        return my_attrs

    def get_input_datatype(self):
        """Returns FINN DataType of input."""
        return DataType[self.get_nodeattr("dataType")]

    def get_output_datatype(self):
        """Returns FINN DataType of output."""
        return DataType[self.get_nodeattr("dataType")]

    def get_normal_input_shape(self):
        ishape = self.get_nodeattr("shape")
        return ishape

    def get_normal_output_shape(self):
        oshape = self.get_nodeattr("shape")
        return oshape

    def get_folded_input_shape(self):
        # for correct functionality of the dwc node the
        # following must apply:
        # if inWidth > outWidth: inWidth % outWidth = 0
        # if inWidth < outWidth: outWidth % inWidth = 0
        iwidth = self.get_nodeattr("inWidth")
        owidth = self.get_nodeattr("outWidth")
        if iwidth > owidth:
            assert (
                iwidth % owidth == 0
            ), """InWidth is bigger than OutWidth and is not divisible by it.
            Please adjust PE and SIMD values so that InWidth % OutWidth = 0"""
        else:
            assert (
                owidth % iwidth == 0
            ), """OutWidth is bigger than InWidth and is not divisible by it.
            Please adjust PE and SIMD values so that OutWidth % InWidth = 0"""

        ishape = self.get_normal_input_shape()
        dummy_t = np.random.randn(*ishape)
        ibits = self.get_input_datatype().bitwidth()
        assert (
            iwidth % ibits == 0
        ), """DWC input width must be divisible by
        input element bitwidth"""
        ielems = int(iwidth // ibits)
        ichannels = ishape[-1]
        new_shape = []
        for i in ishape[:-1]:
            new_shape.append(i)
        new_shape.append(int(ichannels // ielems))
        new_shape.append(ielems)
        dummy_t = dummy_t.reshape(new_shape)
        return dummy_t.shape

    def get_folded_output_shape(self):
        # for correct functionality of the dwc node the
        # following must apply:
        # if inWidth > outWidth: inWidth % outWidth = 0
        # if inWidth < outWidth: outWidth % inWidth = 0
        iwidth = self.get_nodeattr("inWidth")
        owidth = self.get_nodeattr("outWidth")
        if iwidth > owidth:
            assert (
                iwidth % owidth == 0
            ), """InWidth is bigger than OutWidth and is not divisible by it.
            Please adjust PE and SIMD values so that InWidth % OutWidth = 0"""
        else:
            assert (
                owidth % iwidth == 0
            ), """OutWidth is bigger than InWidth and is not divisible by it.
            Please adjust PE and SIMD values so that OutWidth % InWidth = 0"""

        oshape = self.get_normal_output_shape()
        dummy_t = np.random.randn(*oshape)
        obits = self.get_output_datatype().bitwidth()
        assert (
            owidth % obits == 0
        ), """DWC output width must be divisible by
        input element bitwidth"""
        oelems = int(owidth // obits)
        ochannels = oshape[-1]
        new_shape = []
        for i in oshape[:-1]:
            new_shape.append(i)
        new_shape.append(int(ochannels // oelems))
        new_shape.append(oelems)
        dummy_t = dummy_t.reshape(new_shape)

        return dummy_t.shape

    def get_number_output_values(self):
        folded_oshape = self.get_folded_output_shape()
        return np.prod(folded_oshape[:-1])

    def get_number_input_values(self):
        folded_ishape = self.get_folded_input_shape()
        return np.prod(folded_ishape[:-1])

    def get_instream_width(self, axi_strm_padding=False):
        in_width = self.get_nodeattr("inWidth")
        if axi_strm_padding is True:
            in_width = roundup_to_integer_multiple(in_width, 8)
        return in_width

    def get_outstream_width(self, axi_strm_padding=False):
        out_width = self.get_nodeattr("outWidth")
        if axi_strm_padding is True:
            out_width = roundup_to_integer_multiple(out_width, 8)
        return out_width

    def make_shape_compatible_op(self, model):
        exp_ishape = self.get_normal_input_shape()
        oshape = self.get_normal_output_shape()
        ishape = tuple(model.get_tensor_shape(self.onnx_node.input[0]))
        assert ishape == tuple(exp_ishape), "Unexpect input shape for StreamingDWC."
        # implement tensor with correct shape
        values = np.random.randn(*oshape).astype(np.float32)
        return helper.make_node(
            "Constant",
            inputs=[],
            outputs=[self.onnx_node.output[0]],
            value=helper.make_tensor(
                name="const_tensor",
                data_type=TensorProto.FLOAT,
                dims=values.shape,
                vals=values.flatten().astype(float),
            ),
        )

    def infer_node_datatype(self, model):
        node = self.onnx_node
        # data type stays the same
        dtype = model.get_tensor_datatype(node.input[0])
        model.set_tensor_datatype(node.output[0], dtype)

    def verify_node(self):
        info_messages = []

        # verify that "domain" is set to "finn"
        domain_value = self.onnx_node.domain
        if domain_value == "finn":
            info_messages.append("Attribute domain is set correctly")
        else:
            info_messages.append('Attribute domain should be set to "finn"')

        # verify that "backend" is set to "fpgadataflow"
        backend_value = self.get_nodeattr("backend")
        if backend_value == "fpgadataflow":
            info_messages.append("Attribute backend is set correctly")
        else:
            info_messages.append('Attribute backend should be set to "fpgadataflow"')

        # verify the number of inputs
        if len(self.onnx_node.input) == 1:
            info_messages.append("The number of inputs is correct")
        else:
            info_messages.append("""StreamingDWC needs 1 data input""")

        return info_messages

    def global_includes(self):
        self.code_gen_dict["$GLOBALS$"] = ['#include "streamtools.h"']

    def defines(self, var):
        numReps = 1
        numInWords = 1
        inWidth = self.get_nodeattr("inWidth")
        outWidth = self.get_nodeattr("outWidth")
        if outWidth > inWidth:
            numInWords = int(outWidth // inWidth)
        self.code_gen_dict["$DEFINES$"] = [
            "#define InWidth %d " % inWidth,
            "#define OutWidth %d " % outWidth,
            "#define NumInWords %d " % numInWords,
            "#define numReps %d" % numReps,
        ]

    def read_npy_data(self):
        code_gen_dir = self.get_nodeattr("code_gen_dir_npysim")
        dtype = self.get_input_datatype()
        if dtype == DataType.BIPOLAR:
            # use binary for bipolar storage
            dtype = DataType.BINARY
        elem_bits = dtype.bitwidth()
        packed_bits = self.get_instream_width()
        packed_hls_type = "ap_uint<%d>" % packed_bits
        elem_hls_type = dtype.get_hls_datatype_str()
        npy_type = "float"
        npy_in = "%s/input_0.npy" % code_gen_dir
        self.code_gen_dict["$READNPYDATA$"] = []
        self.code_gen_dict["$READNPYDATA$"].append(
            'npy2apintstream<%s, %s, %d, %s>("%s", in0);'
            % (packed_hls_type, elem_hls_type, elem_bits, npy_type, npy_in)
        )

    def strm_decl(self):
        self.code_gen_dict["$STREAMDECLARATIONS$"] = []
        self.code_gen_dict["$STREAMDECLARATIONS$"].append(
            'hls::stream<ap_uint<{}>> in0 ("in0");'.format(self.get_instream_width())
        )
        self.code_gen_dict["$STREAMDECLARATIONS$"].append(
            'hls::stream<ap_uint<{}>> out ("out");'.format(self.get_outstream_width())
        )

    def docompute(self):
        # TODO continue with fxns below, they are copy-pasted
        op = "StreamingDataWidthConverter_Batch"
        self.code_gen_dict["$DOCOMPUTE$"] = [
            "%s<InWidth, OutWidth, NumInWords>(in0, out, numReps);" % (op)
        ]

    def dataoutstrm(self):
        code_gen_dir = self.get_nodeattr("code_gen_dir_npysim")
        dtype = self.get_output_datatype()
        if dtype == DataType.BIPOLAR:
            # use binary for bipolar storage
            dtype = DataType.BINARY
        elem_bits = dtype.bitwidth()
        packed_bits = self.get_outstream_width()
        packed_hls_type = "ap_uint<%d>" % packed_bits
        elem_hls_type = dtype.get_hls_datatype_str()
        npy_type = "float"
        npy_out = "%s/output.npy" % code_gen_dir
        oshape = self.get_folded_output_shape()
        oshape_cpp_str = str(oshape).replace("(", "{").replace(")", "}")

        self.code_gen_dict["$DATAOUTSTREAM$"] = [
            'apintstream2npy<%s, %s, %d, %s>(out, %s, "%s");'
            % (
                packed_hls_type,
                elem_hls_type,
                elem_bits,
                npy_type,
                oshape_cpp_str,
                npy_out,
            )
        ]

    def save_as_npy(self):
        self.code_gen_dict["$SAVEASCNPY$"] = []

    def blackboxfunction(self):
        in_packed_bits = self.get_instream_width()
        in_packed_hls_type = "ap_uint<%d>" % in_packed_bits
        out_packed_bits = self.get_outstream_width()
        out_packed_hls_type = "ap_uint<%d>" % out_packed_bits
        self.code_gen_dict["$BLACKBOXFUNCTION$"] = [
            "void %s(hls::stream<%s > &in0, hls::stream<%s > &out)"
            % (self.onnx_node.name, in_packed_hls_type, out_packed_hls_type)
        ]

    def pragmas(self):
        self.code_gen_dict["$PRAGMAS$"] = ["#pragma HLS INTERFACE axis port=in0"]
        self.code_gen_dict["$PRAGMAS$"].append("#pragma HLS INTERFACE axis port=out")
        self.code_gen_dict["$PRAGMAS$"].append(
            "#pragma HLS INTERFACE ap_ctrl_none port=return"
        )

    def execute_node(self, context, graph):
        mode = self.get_nodeattr("exec_mode")
        node = self.onnx_node
        exp_shape = self.get_normal_input_shape()
        folded_ishape = self.get_folded_input_shape()

        # TODO ensure codegen dir exists
        if mode == "npysim":
            code_gen_dir = self.get_nodeattr("code_gen_dir_npysim")
        elif mode == "rtlsim":
            code_gen_dir = self.get_nodeattr("code_gen_dir_ipgen")
        else:
            raise Exception(
                """Invalid value for attribute exec_mode! Is currently set to: {}
            has to be set to one of the following value ("npysim", "rtlsim")""".format(
                    mode
                )
            )

        inp = context[node.input[0]]
        assert str(inp.dtype) == "float32", "Input datatype is not float32"
        assert inp.shape == tuple(
            exp_shape
        ), "Input shape does not match expected shape."

        if self.get_input_datatype() == DataType.BIPOLAR:
            # store bipolar activations as binary
            inp = (inp + 1) / 2
            export_idt = DataType.BINARY
        else:
            export_idt = self.get_input_datatype()
        # reshape input into folded shape
        reshaped_input = inp.reshape(folded_ishape)
        # make copy before saving array
        reshaped_input = reshaped_input.copy()
        np.save(os.path.join(code_gen_dir, "input_0.npy"), reshaped_input)

        if mode == "npysim":
            output = inp
            output = np.asarray([output], dtype=np.float32).reshape(*exp_shape)
            context[node.output[0]] = output

        elif mode == "rtlsim":
            if PyVerilator is None:
                raise ImportError("Installation of PyVerilator is required.")

            prefixed_top_name = "%s_%s" % (node.name, node.name)
            # check if needed file exists
            verilog_file = "{}/project_{}/sol1/impl/verilog/{}.v".format(
                code_gen_dir, node.name, prefixed_top_name
            )
            if os.path.isfile(verilog_file):
                nbits = self.get_instream_width()
                rtlsim_inp = npy_to_rtlsim_input(
                    "{}/input_0.npy".format(code_gen_dir), export_idt, nbits
                )
                sim = PyVerilator.build(
                    verilog_file,
                    verilog_path=[
                        "{}/project_{}/sol1/impl/verilog/".format(
                            code_gen_dir, node.name
                        )
                    ],
                )
                super().reset_rtlsim(sim)
                super().toggle_clk(sim)
                rtlsim_output = self.rtlsim(sim, rtlsim_inp)
                odt = export_idt
                target_bits = odt.bitwidth()
                packed_bits = self.get_outstream_width()
                out_npy_path = "{}/output.npy".format(code_gen_dir)
                out_shape = self.get_folded_output_shape()
                rtlsim_output_to_npy(
                    rtlsim_output,
                    out_npy_path,
                    odt,
                    out_shape,
                    packed_bits,
                    target_bits,
                )
                # load and reshape output
                output = np.load(out_npy_path)
                output = np.asarray([output], dtype=np.float32).reshape(exp_shape)
                context[node.output[0]] = output
            else:
                raise Exception(
                    """Found no verilog files for this node,
                    did you run the codegen_ipgen transformation?"""
                )
        else:
            raise Exception(
                """Invalid value for attribute exec_mode! Is currently set to: {}
            has to be set to "rtlsim" """.format(
                    mode
                )
            )
        # binary -> bipolar if needed
        if self.get_output_datatype() == DataType.BIPOLAR:
            out = context[node.output[0]]
            out = 2 * out - 1
            context[node.output[0]] = out
        assert context[node.output[0]].shape == tuple(
            exp_shape
        ), """Output
        shape doesn't match expected shape, should be same as input shape"""