from types import NoneType
from typing import Any, Optional, Union

from kernelfunctions.backend import ModifierID
from kernelfunctions.typeregistry import get_or_create_type

from .enums import PrimType, IOType
from .codegen import CodeGen
from .native import AccessType, CallMode, Shape
from .basetype import BindContext
from .reflection import BaseSlangVariable, SlangFunction, SlangType


class BoundVariableException(Exception):
    def __init__(self, message: str, variable: 'BoundVariable') -> NoneType:
        super().__init__(message)
        self.message = message
        self.variable = variable


class BoundCall:
    def __init__(self, context: 'BindContext', *args: Any, **kwargs: Any) -> NoneType:
        super().__init__()
        self.args = [BoundVariable(context, x, None, "") for x in args]
        self.kwargs = {n: BoundVariable(context, v, None, n) for n, v in kwargs.items()}

    def bind(self, slang: SlangFunction):
        self.slang = slang

    @property
    def differentiable(self) -> bool:
        return self.slang.differentiable

    @property
    def num_function_args(self) -> int:
        total = len(self.args) + self.num_function_kwargs
        return total

    @property
    def num_function_kwargs(self) -> int:
        total = len(self.kwargs)
        if "_this" in self.kwargs:
            total -= 1
        if "_result" in self.kwargs:
            total -= 1
        return total

    @property
    def has_implicit_args(self) -> bool:
        return any(x.vector_type is None for x in self.args)

    @property
    def has_implicit_mappings(self) -> bool:
        return any(not x.vector_mapping.valid for x in self.args)

    def apply_explicit_vectorization(self, context: 'BindContext', args: tuple[Any, ...], kwargs: dict[str, Any]):
        if len(args) > len(self.args):
            raise ValueError("Too many arguments supplied for explicit vectorization")
        if len(kwargs) > len(self.kwargs):
            raise ValueError(
                "Too many keyword arguments supplied for explicit vectorization")

        for i, arg in enumerate(args):
            self.args[i].apply_explicit_vectorization(context, arg)

        for name, arg in kwargs.items():
            if not name in self.kwargs:
                raise ValueError(f"Unknown keyword argument {name}")
            self.kwargs[name].apply_explicit_vectorization(context, arg)

    def values(self) -> list['BoundVariable']:
        return self.args + list(self.kwargs.values())

    def apply_implicit_vectorization(self, context: BindContext):
        for arg in self.args:
            arg.apply_implicit_vectorization(context)

        for arg in self.kwargs.values():
            arg.apply_implicit_vectorization(context)

    def finalize_mappings(self, context: BindContext):
        for arg in self.args:
            arg.finalize_mappings(context)

        for arg in self.kwargs.values():
            arg.finalize_mappings(context)


class BoundVariable:
    """
    Node in a built signature tree, maintains a pairing of python+slang marshall,
    and a potential set of child nodes
    """

    def __init__(self,
                 context: 'BindContext',
                 value: Any,
                 parent: Optional['BoundVariable'],
                 name: str):

        super().__init__()

        # Store the python and slang marshall
        self.name = name
        self.python = get_or_create_type(context.layout, type(value), value)

        # Init default properties
        self.access = (AccessType.none, AccessType.none)
        self.differentiable = False
        self.call_dimensionality = None
        self.param_index = -1
        self.vector_mapping: Shape = Shape(None)
        self.vector_type: Optional[SlangType] = None
        self.explicitly_vectorized = False
        self.slang_type: Optional[SlangType] = None

        # Initialize path
        if parent is None:
            self.path = self.name
        else:
            self.path = f"{parent.path}.{self.name}"

        # Create children
        # TODO: Should this be based off type fields
        if isinstance(value, dict):
            self.children = {n: BoundVariable(context, v, self, n)
                             for n, v in value.items()}
        else:
            self.children = None

    def bind(self, slang: Union[BaseSlangVariable, SlangType], modifiers: set[ModifierID] = set(), override_name: Optional[str] = None):
        if isinstance(slang, SlangType):
            if self.name == '':
                assert override_name is not None
                self.name = override_name
            else:
                self.name = self.name
            self.slang_type = slang
            self.slang_modifiers = modifiers
        else:
            self.name = slang.name
            self.slang_type = slang.type
            self.slang_modifiers = modifiers.union(slang.modifiers)
        self.variable_name = self.name

        if self.children is not None:
            for child in self.children.values():
                slang_child = self.slang_type.fields[child.name]
                child.bind(slang_child, self.slang_modifiers)

    @property
    def io_type(self) -> IOType:
        have_in = ModifierID.inn in self.slang_modifiers
        have_out = ModifierID.out in self.slang_modifiers
        have_inout = ModifierID.inout in self.slang_modifiers

        if (have_in and have_out) or have_inout:
            return IOType.inout
        elif have_out:
            return IOType.out
        else:
            return IOType.inn

    @property
    def no_diff(self) -> bool:
        return ModifierID.nodiff in self.slang_modifiers

    def apply_explicit_vectorization(self, context: 'BindContext', mapping: Any):
        """
        Apply explicit vectorization to this variable and children.
        This will result in any explicit mapping or typing provided
        by the caller being stored on the corresponding bound variable.
        """
        if self.children is not None:
            assert isinstance(mapping, dict)
            for name, child in self.children.items():
                child_mapping = mapping.get(name)
                if child_mapping is not None:
                    assert isinstance(child, BoundVariable)
                    child.apply_explicit_vectorization(context, child_mapping)

            type_mapping = mapping.get("$type")
            if type_mapping is not None:
                self._apply_explicit_vectorization(context, type_mapping)
        else:
            self._apply_explicit_vectorization(context, mapping)

    def _apply_explicit_vectorization(self, context: 'BindContext', mapping: Any):
        try:
            if isinstance(mapping, tuple):
                self.vector_mapping = Shape(*mapping)
                self.vector_type = self.python.reduce_type(context, len(mapping))
                self.explicitly_vectorized = True
            elif isinstance(mapping, SlangType):
                self.vector_type = mapping
                self.explicitly_vectorized = True
            elif isinstance(mapping, str):
                self.vector_type = context.layout.find_type_by_name(mapping)
                self.explicitly_vectorized = True
            elif isinstance(mapping, type):
                marshall = get_or_create_type(context.layout, mapping)
                if not marshall:
                    raise BoundVariableException(
                        f"Invalid explicit type: {mapping}", self)
                self.vector_type = marshall.slang_type
                self.explicitly_vectorized = True
            else:
                raise BoundVariableException(
                    f"Invalid explicit type: {mapping}", self)
        except Exception as e:
            raise BoundVariableException(
                f"Explicit vectorization raised exception: {e.__repr__()}", self)

    def apply_implicit_vectorization(self, context: BindContext):
        """
        Apply implicit vectorization to this variable. This inspects
        the slang type being bound to in an attempt to get a concrete
        type to provide to the specialization system.
        """
        if self.children is not None:
            for child in self.children.values():
                child.apply_implicit_vectorization(context)
        self._apply_implicit_vectorization(context)

    def _apply_implicit_vectorization(self, context: BindContext):
        if self.vector_mapping.valid:
            # if we have a valid vector mapping, just need to reduce it
            self.vector_type = self.python.reduce_type(context, len(self.vector_mapping))

        if self.vector_type is not None:
            # do nothing in first phase if already have a type. vector
            # mapping will be worked out once specialized slang function is known
            pass
        elif self.path == '_result':
            # result is inferred last
            pass
        else:
            # neither specified, attempt to resolve type
            assert self.slang_type is not None
            self.vector_type = self.python.resolve_type(
                context, self.slang_type)

        # If we ended up with no valid type, use slang type. Currently this should
        # only happen for auto-allocated result buffers
        if not self.vector_mapping.valid and self.vector_type is None:
            assert self.path == '_result'
            self.vector_type = self.slang_type

        # Clear slang type info - it should never be used after this
        # Note: useful for debugging so keeping for now!
        # self.slang_type = None

        # Can now calculate dimensionality
        if self.vector_mapping.valid:
            if len(self.vector_mapping) > 0:
                self.call_dimensionality = max(
                    self.vector_mapping.as_tuple())+1
            else:
                self.call_dimensionality = 0
        else:
            assert self.vector_type is not None
            self.call_dimensionality = self.python.resolve_dimensionality(
                context, self.vector_type)

    def finalize_mappings(self, context: BindContext):
        """
        Finalize vector mappings and types for this variable and children.
        """
        if self.children is not None:
            for child in self.children.values():
                child.finalize_mappings(context)
        self._finalize_mappings(context)

    def _finalize_mappings(self, context: BindContext):
        if context.options['strict_broadcasting'] and self.children is None and not self.explicitly_vectorized:
            if self.call_dimensionality != 0 and self.call_dimensionality != context.call_dimensionality:
                raise BoundVariableException(
                    f"Strict broadcasting is enabled and {self.path} dimensionality ({self.call_dimensionality}) is neither 0 or the kernel dimensionality ({context.call_dimensionality})", self)

        if not self.vector_mapping.valid:
            assert self.call_dimensionality is not None
            m: list[int] = []
            for i in range(self.call_dimensionality):
                m.append(context.call_dimensionality - i - 1)
            m.reverse()
            self.vector_mapping = Shape(*m)

    def calculate_differentiability(self, context: BindContext):
        """
        Recursively calculate  differentiability
        """

        # Can now decide if differentiable
        assert self.vector_type is not None
        self.differentiable = not self.no_diff and self.vector_type.differentiable and self.python.has_derivative
        self._calculate_differentiability(context.call_mode)

        if self.children is not None:
            for child in self.children.values():
                child.calculate_differentiability(context)

    def get_input_list(self, args: list['BoundVariable']):
        """
        Recursively populate flat list of argument nodes
        """
        self._get_input_list_recurse(args)
        return args

    def _get_input_list_recurse(self, args: list['BoundVariable']):
        """
        Internal recursive function to populate flat list of argument nodes
        """
        if self.children is not None:
            for child in self.children.values():
                child._get_input_list_recurse(args)
        else:
            args.append(self)

    def __repr__(self):
        return self.python.__repr__()

    def _calculate_differentiability(self, mode: CallMode):
        """
        Calculates access types based on differentiability, call mode and io type
        """
        if mode == CallMode.prim:
            if self.differentiable:
                if self.io_type == IOType.inout:
                    self.access = (AccessType.readwrite, AccessType.none)
                elif self.io_type == IOType.out:
                    self.access = (AccessType.write, AccessType.none)
                else:
                    self.access = (AccessType.read, AccessType.none)
            else:
                if self.io_type == IOType.inout:
                    self.access = (AccessType.readwrite, AccessType.none)
                elif self.io_type == IOType.out:
                    self.access = (AccessType.write, AccessType.none)
                else:
                    self.access = (AccessType.read, AccessType.none)
        elif mode == CallMode.bwds:
            if self.differentiable:
                if self.io_type == IOType.inout:
                    self.access = (AccessType.read, AccessType.readwrite)
                elif self.io_type == IOType.out:
                    self.access = (AccessType.none, AccessType.read)
                else:
                    self.access = (AccessType.read, AccessType.write)
            else:
                if self.io_type == IOType.inout:
                    self.access = (AccessType.read, AccessType.none)
                elif self.io_type == IOType.out:
                    self.access = (AccessType.none, AccessType.none)
                else:
                    self.access = (AccessType.read, AccessType.none)
        else:
            # todo: fwds
            self.access = (AccessType.none, AccessType.none)

    def gen_call_data_code(self, cg: CodeGen, context: BindContext, depth: int = 0):
        if self.children is not None:
            cgb = cg.call_data_structs

            cgb.begin_struct(f"_t_{self.variable_name}")

            names: list[tuple[Any, ...]] = []
            for field, variable in self.children.items():
                variable_name = variable.gen_call_data_code(cg, context, depth+1)
                if variable_name is not None:
                    assert variable.vector_type is not None
                    names.append(
                        (field, variable_name, variable.vector_type.full_name, variable.vector_type.full_name + ".Differential"))

            for name in names:
                cgb.declare(f"_t_{name[1]}", name[1])

            for prim in PrimType:
                if self.access[prim.value] == AccessType.none:
                    continue

                assert self.vector_type is not None
                prim_name = prim.name
                prim_type_name = self.vector_type.full_name
                if prim != PrimType.primal:
                    prim_type_name += ".Differential"

                cgb.empty_line()

                cgb.empty_line()
                cgb.append_line(
                    f"void load_{prim_name}(IContext context, out {prim_type_name} value)")
                cgb.begin_block()
                for name in names:
                    cgb.declare(name[2], name[0])
                    cgb.append_statement(
                        f"this.{name[1]}.load_{prim_name}(ctx(context, _m_{name[1]}),{name[0]})")
                    cgb.assign(f"value.{name[0]}", f"{name[0]}")
                cgb.end_block()

                cgb.empty_line()
                cgb.append_line(
                    f"void store_{prim_name}(IContext context, in {prim_type_name} value)")
                cgb.begin_block()
                for name in names:
                    cgb.append_statement(
                        f"this.{name[1]}.store_{prim_name}(ctx(context, _m_{name[1]}),value.{name[0]})")
                cgb.end_block()

            cgb.end_struct()

            full_map = list(range(context.call_dimensionality))
            if len(full_map) > 0:
                cg.call_data_structs.append_statement(
                    f"static const int[] _m_{self.variable_name} = {{ {','.join([str(x) for x in full_map])} }}")
            else:
                cg.call_data_structs.append_statement(
                    f"static const int _m_{self.variable_name} = 0")

        else:
            # Raise error if attempting to write to non-writable type
            if self.access[0] in [AccessType.write, AccessType.readwrite] and not self.python.is_writable:
                if depth == 0:
                    raise BoundVariableException(
                        f"Cannot read back value for non-writable type", self)

            # Generate call data
            self.python.gen_calldata(cg.call_data_structs, context, self)

            if len(self.vector_mapping) > 0:
                cg.call_data_structs.append_statement(
                    f"static const int[] _m_{self.variable_name} = {{ {','.join([str(x) for x in self.vector_mapping.as_tuple()])} }}")
            else:
                cg.call_data_structs.append_statement(
                    f"static const int _m_{self.variable_name} = 0")

        if depth == 0:
            cg.call_data.declare(f"_t_{self.variable_name}", self.variable_name)

        return self.variable_name

    def _gen_trampoline_argument(self):
        assert self.vector_type is not None
        arg_def = f"{self.vector_type.full_name} {self.variable_name}"
        if self.io_type == IOType.inout:
            arg_def = f"inout {arg_def}"
        elif self.io_type == IOType.out:
            arg_def = f"out {arg_def}"
        elif self.io_type == IOType.inn:
            arg_def = f"in {arg_def}"
        if self.no_diff or not self.differentiable:
            arg_def = f"no_diff {arg_def}"
        return arg_def

    def __str__(self) -> str:
        return self._recurse_str(0)

    def _recurse_str(self, depth: int) -> str:
        if self.children is not None:
            child_strs = [
                f"{'  ' * depth}{name}: {child._recurse_str(depth + 1)}" for name, child in self.children.items()]
            return "\n" + "\n".join(child_strs)
        else:
            return f"{self.name}"
