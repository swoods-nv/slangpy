

from typing import Any, Optional, Sequence

from sgl import TypeReflection

from kernelfunctions.bindings.diffpairtype import generate_differential_pair
from kernelfunctions.core import CodeGenBlock, BaseType, BaseTypeImpl, BoundVariable, AccessType, PrimType, BoundVariableRuntime

from kernelfunctions.shapes import TLooseShape
from kernelfunctions.types import NDBuffer, NDDifferentiableBuffer

from kernelfunctions.backend import Device, ResourceUsage
from kernelfunctions.typeregistry import PYTHON_TYPES, SLANG_STRUCT_TYPES_BY_NAME, get_or_create_type
from kernelfunctions.utils import parse_generic_signature


class NDBufferType(BaseTypeImpl):

    def __init__(self, element_type: BaseType, dims: int, writable: bool):
        super().__init__()
        self.el_type = element_type
        self.dims = dims
        self.writable = writable

    # Values don't store a derivative - they're just a value
    @property
    def has_derivative(self) -> bool:
        return False

    @property
    def is_writable(self) -> bool:
        return self.writable

    # Call data can only be read access to primal, and simply declares it as a variable
    def gen_calldata(self, cgb: CodeGenBlock, binding: 'BoundVariable'):
        access = binding.access
        name = binding.variable_name
        assert access[0] != AccessType.none
        assert access[1] == AccessType.none
        if access[0] == AccessType.read:
            cgb.type_alias(
                f"_{name}", f"NDBuffer<{self.el_type.name},{self.dims}>")
        else:
            cgb.type_alias(
                f"_{name}", f"RWNDBuffer<{self.el_type.name},{self.dims}>")

    # Call data just returns the primal

    def create_calldata(self, device: Device, binding: 'BoundVariableRuntime', broadcast: list[bool], data: NDBuffer) -> Any:
        return {
            'buffer': data.buffer,
            'strides': [data.strides[i] if not broadcast[i] else 0 for i in range(len(data.strides))],
            'transform': binding.transform[0:self.dims]  # type: ignore
        }

    @property
    def name(self) -> str:
        if not self.writable:
            return f"NDBuffer<{self.el_type.name},{self.dims}>"
        else:
            return f"RWNDBuffer<{self.el_type.name},{self.dims}>"

    @property
    def element_type(self):
        return self.el_type

    def get_container_shape(self, value: Optional[NDDifferentiableBuffer] = None) -> TLooseShape:
        if value is not None:
            return value.shape
        else:
            return (None,)*self.dims

    @property
    def differentiable(self):
        return self.el_type.differentiable

    @property
    def derivative(self):
        et = self.el_type
        if et is not None:
            return NDBufferType(et, self.dims, self.writable)
        else:
            return None

    def create_output(self, device: Device, call_shape: Sequence[int]) -> Any:
        return NDBuffer(device, self.el_type.python_return_value_type, shape=tuple(call_shape), usage=ResourceUsage.shader_resource | ResourceUsage.unordered_access)

    def read_output(self, device: Device, data: NDDifferentiableBuffer) -> Any:
        return data


def create_vr_type_for_value(value: NDBuffer):
    assert isinstance(value, NDBuffer)
    return NDBufferType(get_or_create_type(value.element_type), len(value.shape), (value.usage & ResourceUsage.unordered_access) != 0)


def create_vr_type_for_slang(value: TypeReflection):
    assert isinstance(value, TypeReflection)
    name, args = parse_generic_signature(value.full_name)
    return NDBufferType(get_or_create_type(args[0]), int(args[1]), name.startswith("RW"))


PYTHON_TYPES[NDBuffer] = create_vr_type_for_value
SLANG_STRUCT_TYPES_BY_NAME["NDBuffer"] = create_vr_type_for_slang


class NDDifferentiableBufferType(BaseTypeImpl):

    def __init__(self, element_type: BaseType, dims: int, writable: bool):
        super().__init__()
        self.el_type = element_type
        self.dims = dims
        self.writable = writable

    # Values don't store a derivative - they're just a value
    @property
    def has_derivative(self) -> bool:
        return True

    @property
    def is_writable(self) -> bool:
        return self.writable

    # Call data can only be read access to primal, and simply declares it as a variable
    def gen_calldata(self, cgb: CodeGenBlock, binding: 'BoundVariable'):
        access = binding.access
        name = binding.variable_name

        prim_el = binding.python.primal_element_name
        deriv_el = binding.python.derivative_element_name
        if deriv_el is None:
            deriv_el = prim_el
        dim = self.dims

        if access[0] == AccessType.none:
            primal_storage = f'NoneType'
        elif access[0] == AccessType.read:
            primal_storage = f"NDBuffer<{prim_el},{dim}>"
        else:
            primal_storage = f"RWNDBuffer<{prim_el},{dim}>"

        if access[1] == AccessType.none:
            deriv_storage = f'NoneType'
        elif access[1] == AccessType.read:
            deriv_storage = f"NDBuffer<{deriv_el},{dim}>"
        else:
            deriv_storage = f"RWNDBuffer<{deriv_el},{dim}>"

        primal_target = binding.slang.primal_type_name
        deriv_target = binding.slang.derivative_type_name

        cgb.append_code(generate_differential_pair(name, primal_storage,
                        deriv_storage, primal_target, deriv_target))

    # Call data just returns the primal

    def create_calldata(self, device: Device, binding: 'BoundVariableRuntime', broadcast: list[bool], data: NDDifferentiableBuffer) -> Any:
        access = binding.access
        assert binding.transform is not None
        res = {}
        for prim in PrimType:
            prim_name = prim.name
            prim_access = access[prim.value]
            if prim_access != AccessType.none:
                ndbuffer = data if prim == PrimType.primal else data.grad
                assert ndbuffer is not None
                value = ndbuffer.buffer if prim == PrimType.primal else ndbuffer.buffer
                res[prim_name] = {
                    'buffer': value,
                    'strides': [data.strides[i] if not broadcast[i] else 0 for i in range(len(data.strides))],
                    'transform': binding.transform[0:self.dims]
                }
        return res

    @property
    def name(self) -> str:
        if not self.writable:
            return f"NDBuffer<{self.el_type.name},{self.dims}>"
        else:
            return f"RWNDBuffer<{self.el_type.name},{self.dims}>"

    @property
    def element_type(self):
        return self.el_type

    def get_container_shape(self, value: Optional[NDDifferentiableBuffer] = None) -> TLooseShape:
        if value is not None:
            return value.shape
        else:
            return (None,)*self.dims

    @property
    def differentiable(self):
        return self.el_type.differentiable

    @property
    def derivative(self):
        et = self.el_type.derivative
        if et is not None:
            return NDDifferentiableBufferType(et, self.dims, self.writable)
        else:
            return None

    def create_output(self, device: Device, call_shape: Sequence[int]) -> Any:
        return NDDifferentiableBuffer(device, self.el_type.python_return_value_type,
                                      shape=tuple(call_shape),
                                      requires_grad=True,
                                      usage=ResourceUsage.shader_resource | ResourceUsage.unordered_access)

    def read_output(self, device: Device, data: NDDifferentiableBuffer) -> Any:
        return data


def create_gradvr_type_for_value(value: Any):
    assert isinstance(value, NDDifferentiableBuffer)
    return NDDifferentiableBufferType(get_or_create_type(value.element_type), len(value.shape), (value.usage & ResourceUsage.unordered_access) != 0)


PYTHON_TYPES[NDDifferentiableBuffer] = create_gradvr_type_for_value
