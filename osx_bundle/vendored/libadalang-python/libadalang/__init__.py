#
# Copyright (C) 2014-2022, AdaCore
# SPDX-License-Identifier: Apache-2.0
#

"""
Python binding of the Libadalang API.

Please consider all exported entities whose names that start with an underscore
("_") as internal implementation details. They are not meant to be used
directly.
"""







from __future__ import annotations





import argparse
import collections
import ctypes
import io
import json
import os
import re
import sys
import traceback
from typing import (
    Any, AnyStr, Callable, ClassVar, Dict, Generic, IO, Iterator, List,
    Optional as Opt, TYPE_CHECKING, Tuple, Type, TypeVar, Union
)
import weakref


# Protocol was added to "typing" in Python 3.8
if TYPE_CHECKING:
    from typing import Protocol
else:
    Protocol = object


#
# Low-level binding - First part
#

_so_ext = {
    'win32':  'dll',
    'darwin': 'dylib',
}.get(sys.platform, 'so')

# Loading the shared library here is quite involved as we want to support
# Python packages that embed all the required shared libraries: if we can
# find the shared library in the package directory, import it from there
# directly.

# Directory that contains this __init__.py module
_self_path = os.path.dirname(os.path.abspath(__file__))

# Base and full names for the shared library to load. Full name assumes the
# shared lib is in the package directory.
_c_lib_name = 'libadalang.{}'.format(_so_ext)
_c_lib_path = os.path.join(_self_path, _c_lib_name)

# If we can find the shared lirbray in the package directory, load it from
# here, otherwise let the dynamic loader find it in the environment. On
# Windows, there is no RPATH trick, so we need to temporarily alter the PATH
# environment variable in order to import the whole closure of DLLs.
_old_env_path = None
if os.path.exists(_c_lib_path):
    if sys.platform == 'win32':
        _old_env_path = os.environ['PATH']
        os.environ['PATH'] = '{}{}{}'.format(_self_path, os.path.pathsep,
                                             os.environ['PATH'])
else:
    _c_lib_path = _c_lib_name


# Finally load the library
_c_lib = ctypes.cdll.LoadLibrary(_c_lib_path)

# Restore the PATH environment variable if we altered it
if _old_env_path is not None:
    os.environ['PATH'] = _old_env_path


def _import_func(name, argtypes, restype, exc_wrap=True):
    """
    Import "name" from the C library, set its arguments/return types and return
    the binding.

    :param str name: Name of the symbol for the function to import.
    :param list[ctypes._CData] argtypes: Types for function argruments.
    :param None|ctypes._CData restype: Function return type, or None if it
        does not return anything.
    :param bool exc_wrap: If True, wrap the returned function to check for
      exceptions.
    """
    func = getattr(_c_lib, name)
    func.argtypes = argtypes
    func.restype = restype

    def check_argcount(args, kwargs):
        argcount = len(args) + len(kwargs)
        if argcount != len(argtypes):
            raise TypeError(
                '{} takes {} positional arguments but {} was given'
                .format(name, len(argtypes), argcount))

    # Wrapper for "func" that raises a NativeException in case of internal
    # error.

    if exc_wrap:
        def wrapper(*args, **kwargs):
            check_argcount(args, kwargs)
            result = func(*args, **kwargs)
            exc = _get_last_exception()
            if exc:
                raise exc.contents._wrap()
            return result
    else:
        def wrapper(*args, **kwargs):
            check_argcount(args, kwargs)
            return func(*args, **kwargs)

    return wrapper


class _Exception(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_int),
        ("information", ctypes.c_char_p),
        ("stack_trace", ctypes.c_void_p),
    ]

    def _wrap(self):
        # Turn information into native strings, i.e. decode bytes.  These
        # strings are only informative, so do not raise an error if decoding
        # fails: do best effort decoding instead to be as helpful as possible.
        info = self.information.decode(errors='replace')
        return _exception_kind_to_type[self.kind](info)


def _type_fullname(t: type) -> str:
    """
    Return the fully qualified name for the given `t` type.
    """
    name = t.__name__
    module = t.__module__
    return (name
            if module in (None, object.__class__.__module__) else
            '{}.{}'.format(module, name))


def _raise_type_error(expected_type_name: str, actual_value: Any) -> Any:
    raise TypeError('{} instance expected, got {} instead'.format(
        expected_type_name, _type_fullname(type(actual_value))
    ))


def _log_uncaught_error(context):
    """
    Log an uncaught exception on stderr.

    Useful to warn users about an exception that occurs in a Python function
    used as a C callback: we cannot let the exception propagate in this case.
    """
    print(f"Uncaught exception in {context}:", file=sys.stderr)
    traceback.print_exc()


_get_last_exception = _import_func(
   'ada_get_last_exception',
   [], ctypes.POINTER(_Exception),
   exc_wrap=False
)


def _hashable_c_pointer(pointed_type=None):
    """
    Create a "pointer to `pointed_type` type and make it hashable.

    :param pointed_type: ctypes type class. If left to `None`, we return a
        subclass of `ctypes.c_void_p`.
    :rtype: ctypes.POINTER
    """

    if pointed_type is None:
        class _c_type(ctypes.c_void_p):
            @property
            def _pointer_value(self):
                return self.value or 0
    else:
        @property
        def _pointer_value(self):
            return ctypes.cast(self, ctypes.c_void_p).value or 0

        _c_type = ctypes.POINTER(pointed_type)
        _c_type._pointer_value = _pointer_value

    def __hash__(self):
        return self._pointer_value

    def __eq__(self, other):
        return self._pointer_value == other._pointer_value

    def __ne__(self, other):
        return not (self == other)

    _c_type.__hash__ = __hash__
    _c_type.__eq__ = __eq__
    _c_type.__ne__ = __ne__
    return _c_type


def _unwrap_filename(filename: Opt[AnyStr]) -> Opt[bytes]:
    """Turn filename into a suitable C value for filenames."""
    if filename is None:
        return None
    elif isinstance(filename, str):
        return filename.encode()
    elif not isinstance(filename, bytes):
        raise ValueError(f"invalid filename: {filename}")
    else:
        return filename


def _unwrap_charset(charset: Opt[AnyStr]) -> Opt[bytes]:
    """Turn charset into a suitable C value for charsets."""
    if charset is None:
        return None
    elif isinstance(charset, str):
        return charset.encode()
    elif not isinstance(charset, bytes):
        raise ValueError(f"invalid charset: {charset}")
    else:
        return charset


class _text(ctypes.Structure):
    """
    C value for unicode strings. This object is the owner of the underlying
    buffer, so the string will be deallocated when ``self`` is destroyed.

    ``_unwrap`` takes a string/unicode object and returns a ``_text`` instance,
    while ``_wrap`` retuns an unicode instance.
    """
    # The chars field really is a uint32_t* but considering it as a char* here
    # is more convenient for conversion in this binding layer. On the other
    # side, we have to be careful about converting the length when retrieving
    # the chars.
    _fields_ = [("chars", ctypes.POINTER(ctypes.c_char)),
                ("length", ctypes.c_size_t),
                ("is_allocated", ctypes.c_int),]

    encoding = 'utf-32le' if sys.byteorder == 'little' else 'utf-32be'

    # Instances can hold buffers that they own. In this case, the buffer must
    # be deallocated when the instance is destroyed. Thus instances will hold
    # a "text_buffer" attribute that will be automatically destroyed.
    text_buffer = None

    @classmethod
    def _create_buffer(cls, value: AnyStr) -> Tuple[Any, int]:
        """
        Turn `value` into the corresponding UTF-32 string buffer.

        Return both the string buffer and the number of codepoints it contains
        (not the number of bytes!).
        """
        string = cls.cast(value)
        buf = ctypes.create_string_buffer(string.encode(cls.encoding))
        return (buf, len(string))

    @classmethod
    def _decode_buffer(cls, buf: Any, length: int) -> str:
        """
        Decode the UTF-32 string in `buf`.

        :param buf: String buffer (of type `POINTER(c_char_p)`) to decode.
        :param length: Number of codepoints in `buf` (not the number of
            bytes!).
        """
        if length > 0:
            # `length` tells how much UTF-32 chars there are in `buf` but `buf`
            # is a char* so we have to fetch 4 times more bytes than bytes.
            return buf[:4 * length].decode(cls.encoding)
        else:
            return ""

    @classmethod
    def _unwrap(cls, value: AnyStr) -> _text:
        text_buffer, length = cls._create_buffer(value)
        text_buffer_ptr = ctypes.cast(
            ctypes.pointer(text_buffer),
            ctypes.POINTER(ctypes.c_char)
        )
        result = _text(text_buffer_ptr, length)
        result.text_buffer = text_buffer
        return result

    def _wrap(self) -> str:
        return self._decode_buffer(self.chars, self.length)

    @classmethod
    def cast(cls, value: AnyStr) -> str:
        """
        Try to cast ``value`` into an unicode object. Raise a TypeError, or
        raise a string decoding error when this is not possible.
        """
        if isinstance(value, bytes):
            return value.decode('ascii')
        elif not isinstance(value, str):
            _raise_type_error('text string', value)
        else:
            return value

    def __del__(self) -> None:
        _destroy_text(ctypes.byref(self))


class _symbol_type(ctypes.Structure):
    _fields_ = [('thin_symbol', ctypes.c_uint32),
                ('table', ctypes.c_void_p)]

    @classmethod
    def wrap(cls, c_value: Any) -> str:
        # First extract the text associated to this symbol in "text"
        text = _text()
        _symbol_text(ctypes.byref(c_value), ctypes.byref(text))

        # Then wrap this text
        return text._wrap()

    @classmethod
    def unwrap(cls, py_value: AnyStr, context: Any) -> _symbol_type:
        # First turn the given symbol into a low-level text object
        text = _text._unwrap(py_value)

        # Then convert it to a symbol
        result = cls()
        if not _context_symbol(context, ctypes.byref(text),
                               ctypes.byref(result)):
            raise InvalidSymbolError(py_value)
        return result


class _big_integer:

    class c_type(ctypes.c_void_p):
        pass

    def __init__(self, c_value: Any):
        self.c_value = c_value

    @classmethod
    def unwrap(cls, value: int) -> _big_integer:
        if not isinstance(value, int):
            _raise_type_error('int or long', value)

        text = _text._unwrap(str(value))
        c_value = cls.create(ctypes.byref(text))
        return cls(c_value)

    @classmethod
    def wrap(cls, c_value: Any) -> int:
        helper = cls(c_value)
        text = _text()
        cls.text(helper.c_value, ctypes.byref(text))
        return int(text._wrap())

    def clear(self) -> None:
        self.c_value = None

    def __del__(self) -> None:
        self.decref(self.c_value)
        self.clear()

    create = staticmethod(_import_func(
        'ada_create_big_integer',
        [ctypes.POINTER(_text)], c_type
    ))
    text = staticmethod(_import_func(
        'ada_big_integer_text',
        [c_type, ctypes.POINTER(_text)], None
    ))
    decref = staticmethod(_import_func(
        'ada_big_integer_decref',
        [c_type], None
    ))


class _String:
    """
    Helper to wrap/unwrap string values for properties arguments/return types.
    """

    class c_struct(ctypes.Structure):
        _fields_ = [("length", ctypes.c_int),
                    ("ref_count", ctypes.c_int),

                    # See the "chars" field in the _text structure
                    ("content", ctypes.c_char * 1)]
    c_type = ctypes.POINTER(c_struct)

    __slots__ = ("c_value", )

    def __init__(self, c_value):
        self.c_value = c_value

    def __del__(self):
        self.dec_ref(self.c_value)
        self.c_value = None

    @classmethod
    def unwrap(cls, value: AnyStr) -> _String:
        # Convert "value" into the corresponding UTF-32 string buffer
        buf, length = _text._create_buffer(value)
        return cls(cls.create(buf, length))

    @classmethod
    def wrap(cls, value: Any) -> str:
        struct = value.contents

        # "struct.content" will get a one-byte copy of the actual string
        # because of the hack above to handle variable-length struct field. To
        # get the whole string, compute a pointer to this field fierst.
        content_addr = _field_address(struct, "content")
        content = ctypes.pointer(ctypes.c_char.from_address(content_addr))

        return _text._decode_buffer(content, struct.length)

    create = staticmethod(_import_func(
        'ada_create_string',
        [ctypes.POINTER(ctypes.c_char), ctypes.c_int], c_type
    ))
    dec_ref = staticmethod(_import_func(
        'ada_string_dec_ref',
        [c_type], None
    ))


if TYPE_CHECKING:
    _EnumType = TypeVar("_EnumType", bound=_Enum)


class _Enum:

    _name: ClassVar[str]
    """
    Name for this enumeration type.
    """

    _c_to_py: ClassVar[List[str]]
    """
    Mapping from C values to user-level Python values.
    """

    _py_to_c: ClassVar[Dict[str, int]]
    """
    Mapping from user-level Python values to C values.
    """

    @classmethod
    def _unwrap(cls, py_value: str) -> int:
        if not isinstance(py_value, str):
            _raise_type_error('str', py_value)
        try:
            return cls._py_to_c[py_value]
        except KeyError:
            raise ValueError('Invalid {}: {}'.format(cls._name, py_value))

    @classmethod
    def _wrap(cls: Type[_EnumType], c_value: Any) -> _EnumType:
        if isinstance(c_value, ctypes.c_int):
            c_value = c_value.value
        return cls._c_to_py[c_value]


class AnalysisUnitKind(_Enum):
    """
    Specify a kind of analysis unit. Specification units provide an interface
    to the outer world while body units provide an implementation for the
    corresponding interface.
    """

    unit_specification = 'unit_specification'
    unit_body = 'unit_body'

    _name = 'AnalysisUnitKind'
    _c_to_py = [
        unit_specification, unit_body]
    _py_to_c = {name: index for index, name in enumerate(_c_to_py)}
class CallExprKind(_Enum):
    """
    Kind of CallExpr type.

    * ``call`` is when the CallExpr is a procedure or function call.

    * ``array_slice``, ``array_index`` is when the CallExpr is in fact an array
      slice or an array subcomponent access expression, respectively.

    * ``type_conversion`` is when the CallExpr is a type conversion.

    * ``family_index`` is for entry calls using a family index.
    """

    call = 'call'
    array_slice = 'array_slice'
    array_index = 'array_index'
    type_conversion = 'type_conversion'
    family_index = 'family_index'

    _name = 'CallExprKind'
    _c_to_py = [
        call, array_slice, array_index, type_conversion, family_index]
    _py_to_c = {name: index for index, name in enumerate(_c_to_py)}
class CompletionItemKind(_Enum):
    """
    Type of completion item. Refer to the official LSP specification.
    """

    text_kind = 'text_kind'
    method_kind = 'method_kind'
    function_kind = 'function_kind'
    constructor_kind = 'constructor_kind'
    field_kind = 'field_kind'
    variable_kind = 'variable_kind'
    class_kind = 'class_kind'
    interface_kind = 'interface_kind'
    module_kind = 'module_kind'
    property_kind = 'property_kind'
    unit_kind = 'unit_kind'
    value_kind = 'value_kind'
    enum_kind = 'enum_kind'
    keyword_kind = 'keyword_kind'
    snippet_kind = 'snippet_kind'
    color_kind = 'color_kind'
    file_kind = 'file_kind'
    reference_kind = 'reference_kind'
    folder_kind = 'folder_kind'
    enum_member_kind = 'enum_member_kind'
    constant_kind = 'constant_kind'
    struct_kind = 'struct_kind'
    event_kind = 'event_kind'
    operator_kind = 'operator_kind'
    type_parameter_kind = 'type_parameter_kind'

    _name = 'CompletionItemKind'
    _c_to_py = [
        text_kind, method_kind, function_kind, constructor_kind, field_kind, variable_kind, class_kind, interface_kind, module_kind, property_kind, unit_kind, value_kind, enum_kind, keyword_kind, snippet_kind, color_kind, file_kind, reference_kind, folder_kind, enum_member_kind, constant_kind, struct_kind, event_kind, operator_kind, type_parameter_kind]
    _py_to_c = {name: index for index, name in enumerate(_c_to_py)}
class DesignatedEnvKind(_Enum):
    """
    Discriminant for DesignatedEnv structures.
    """

    none = 'none'
    current_env = 'current_env'
    named_env = 'named_env'
    direct_env = 'direct_env'

    _name = 'DesignatedEnvKind'
    _c_to_py = [
        none, current_env, named_env, direct_env]
    _py_to_c = {name: index for index, name in enumerate(_c_to_py)}
class GrammarRule(_Enum):
    """
    Gramar rule to use for parsing.
    """

    parent_list_rule = 'parent_list_rule'
    protected_type_decl_rule = 'protected_type_decl_rule'
    protected_op_rule = 'protected_op_rule'
    protected_el_rule = 'protected_el_rule'
    protected_def_rule = 'protected_def_rule'
    protected_decl_rule = 'protected_decl_rule'
    task_item_rule = 'task_item_rule'
    task_def_rule = 'task_def_rule'
    task_type_decl_rule = 'task_type_decl_rule'
    subtype_decl_rule = 'subtype_decl_rule'
    interface_type_def_rule = 'interface_type_def_rule'
    unconstrained_index_rule = 'unconstrained_index_rule'
    array_type_def_rule = 'array_type_def_rule'
    discrete_subtype_definition_rule = 'discrete_subtype_definition_rule'
    constraint_list_rule = 'constraint_list_rule'
    signed_int_type_def_rule = 'signed_int_type_def_rule'
    mod_int_type_def_rule = 'mod_int_type_def_rule'
    derived_type_def_rule = 'derived_type_def_rule'
    composite_constraint_assoc_rule = 'composite_constraint_assoc_rule'
    composite_constraint_rule = 'composite_constraint_rule'
    digits_constraint_rule = 'digits_constraint_rule'
    delta_constraint_rule = 'delta_constraint_rule'
    range_constraint_rule = 'range_constraint_rule'
    constraint_rule = 'constraint_rule'
    discriminant_spec_rule = 'discriminant_spec_rule'
    discr_spec_list_rule = 'discr_spec_list_rule'
    discriminant_part_rule = 'discriminant_part_rule'
    enum_literal_decl_rule = 'enum_literal_decl_rule'
    formal_discrete_type_def_rule = 'formal_discrete_type_def_rule'
    record_def_rule = 'record_def_rule'
    range_spec_rule = 'range_spec_rule'
    real_type_def_rule = 'real_type_def_rule'
    sexpr_or_box_rule = 'sexpr_or_box_rule'
    ordinary_fixed_point_def_rule = 'ordinary_fixed_point_def_rule'
    decimal_fixed_point_def_rule = 'decimal_fixed_point_def_rule'
    floating_point_def_rule = 'floating_point_def_rule'
    record_type_def_rule = 'record_type_def_rule'
    access_def_rule = 'access_def_rule'
    enum_type_def_rule = 'enum_type_def_rule'
    type_def_rule = 'type_def_rule'
    variant_rule = 'variant_rule'
    anonymous_type_decl_rule = 'anonymous_type_decl_rule'
    incomplete_type_decl_rule = 'incomplete_type_decl_rule'
    type_decl_rule = 'type_decl_rule'
    variant_part_rule = 'variant_part_rule'
    component_def_rule = 'component_def_rule'
    component_item_rule = 'component_item_rule'
    component_decl_rule = 'component_decl_rule'
    component_list_rule = 'component_list_rule'
    generic_decl_rule = 'generic_decl_rule'
    generic_formal_part_rule = 'generic_formal_part_rule'
    generic_formal_decl_rule = 'generic_formal_decl_rule'
    formal_type_decl_rule = 'formal_type_decl_rule'
    formal_subp_decl_rule = 'formal_subp_decl_rule'
    renaming_clause_rule = 'renaming_clause_rule'
    generic_renaming_decl_rule = 'generic_renaming_decl_rule'
    generic_instantiation_rule = 'generic_instantiation_rule'
    exception_decl_rule = 'exception_decl_rule'
    basic_decls_rule = 'basic_decls_rule'
    package_renaming_decl_rule = 'package_renaming_decl_rule'
    package_decl_rule = 'package_decl_rule'
    basic_decl_rule = 'basic_decl_rule'
    object_decl_rule = 'object_decl_rule'
    sub_object_decl_rule = 'sub_object_decl_rule'
    no_type_object_renaming_decl_rule = 'no_type_object_renaming_decl_rule'
    ext_ret_stmt_object_decl_rule = 'ext_ret_stmt_object_decl_rule'
    defining_id_list_rule = 'defining_id_list_rule'
    number_decl_rule = 'number_decl_rule'
    contract_case_assoc_rule = 'contract_case_assoc_rule'
    contract_cases_expr_rule = 'contract_cases_expr_rule'
    abstract_state_decl_rule = 'abstract_state_decl_rule'
    multi_abstract_state_decl_rule = 'multi_abstract_state_decl_rule'
    aspect_assoc_rule = 'aspect_assoc_rule'
    aspect_spec_rule = 'aspect_spec_rule'
    single_task_decl_rule = 'single_task_decl_rule'
    overriding_indicator_rule = 'overriding_indicator_rule'
    entry_decl_rule = 'entry_decl_rule'
    component_clause_rule = 'component_clause_rule'
    aspect_clause_rule = 'aspect_clause_rule'
    param_spec_rule = 'param_spec_rule'
    param_specs_rule = 'param_specs_rule'
    subp_spec_rule = 'subp_spec_rule'
    expr_fn_rule = 'expr_fn_rule'
    null_subp_decl_rule = 'null_subp_decl_rule'
    abstract_subp_decl_rule = 'abstract_subp_decl_rule'
    subp_renaming_decl_rule = 'subp_renaming_decl_rule'
    simple_subp_decl_rule = 'simple_subp_decl_rule'
    subp_decl_rule = 'subp_decl_rule'
    with_clause_rule = 'with_clause_rule'
    context_item_rule = 'context_item_rule'
    use_clause_rule = 'use_clause_rule'
    use_package_clause_rule = 'use_package_clause_rule'
    use_type_clause_rule = 'use_type_clause_rule'
    subtype_indication_rule = 'subtype_indication_rule'
    discrete_subtype_indication_rule = 'discrete_subtype_indication_rule'
    constrained_subtype_indication_rule = 'constrained_subtype_indication_rule'
    type_expr_rule = 'type_expr_rule'
    anonymous_type_rule = 'anonymous_type_rule'
    mode_rule = 'mode_rule'
    pragma_argument_rule = 'pragma_argument_rule'
    pragma_rule = 'pragma_rule'
    subunit_rule = 'subunit_rule'
    library_unit_body_rule = 'library_unit_body_rule'
    library_unit_renaming_decl_rule = 'library_unit_renaming_decl_rule'
    library_item_rule = 'library_item_rule'
    compilation_unit_rule = 'compilation_unit_rule'
    compilation_rule = 'compilation_rule'
    decl_part_rule = 'decl_part_rule'
    entry_body_rule = 'entry_body_rule'
    protected_body_rule = 'protected_body_rule'
    protected_body_stub_rule = 'protected_body_stub_rule'
    task_body_rule = 'task_body_rule'
    task_body_stub_rule = 'task_body_stub_rule'
    package_body_stub_rule = 'package_body_stub_rule'
    package_body_rule = 'package_body_rule'
    terminate_alternative_rule = 'terminate_alternative_rule'
    select_stmt_rule = 'select_stmt_rule'
    accept_stmt_rule = 'accept_stmt_rule'
    case_alt_rule = 'case_alt_rule'
    case_stmt_rule = 'case_stmt_rule'
    ext_return_stmt_rule = 'ext_return_stmt_rule'
    iblock_stmt_rule = 'iblock_stmt_rule'
    block_stmt_rule = 'block_stmt_rule'
    while_loop_spec_rule = 'while_loop_spec_rule'
    iloop_stmt_rule = 'iloop_stmt_rule'
    loop_stmt_rule = 'loop_stmt_rule'
    compound_stmt_rule = 'compound_stmt_rule'
    elsif_part_rule = 'elsif_part_rule'
    if_stmt_rule = 'if_stmt_rule'
    raise_stmt_rule = 'raise_stmt_rule'
    delay_stmt_rule = 'delay_stmt_rule'
    abort_stmt_rule = 'abort_stmt_rule'
    body_rule = 'body_rule'
    body_stub_rule = 'body_stub_rule'
    subp_body_stub_rule = 'subp_body_stub_rule'
    recov_decl_part_rule = 'recov_decl_part_rule'
    subp_body_rule = 'subp_body_rule'
    handled_stmts_rule = 'handled_stmts_rule'
    exception_handler_rule = 'exception_handler_rule'
    stmts_rule = 'stmts_rule'
    label_rule = 'label_rule'
    stmt_rule = 'stmt_rule'
    call_stmt_rule = 'call_stmt_rule'
    simple_stmt_rule = 'simple_stmt_rule'
    simple_decl_stmt_rule = 'simple_decl_stmt_rule'
    null_stmt_rule = 'null_stmt_rule'
    assignment_stmt_rule = 'assignment_stmt_rule'
    goto_stmt_rule = 'goto_stmt_rule'
    exit_stmt_rule = 'exit_stmt_rule'
    return_stmt_rule = 'return_stmt_rule'
    requeue_stmt_rule = 'requeue_stmt_rule'
    identifier_rule = 'identifier_rule'
    char_literal_rule = 'char_literal_rule'
    string_literal_rule = 'string_literal_rule'
    format_string_literal_rule = 'format_string_literal_rule'
    defining_id_rule = 'defining_id_rule'
    dec_literal_rule = 'dec_literal_rule'
    int_literal_rule = 'int_literal_rule'
    num_literal_rule = 'num_literal_rule'
    null_literal_rule = 'null_literal_rule'
    allocator_rule = 'allocator_rule'
    for_loop_param_spec_rule = 'for_loop_param_spec_rule'
    quantified_expr_rule = 'quantified_expr_rule'
    case_expr_rule = 'case_expr_rule'
    case_expr_alt_rule = 'case_expr_alt_rule'
    raise_expr_rule = 'raise_expr_rule'
    if_expr_rule = 'if_expr_rule'
    conditional_expr_rule = 'conditional_expr_rule'
    box_expr_rule = 'box_expr_rule'
    others_designator_rule = 'others_designator_rule'
    iterated_assoc_rule = 'iterated_assoc_rule'
    aggregate_assoc_rule = 'aggregate_assoc_rule'
    regular_aggregate_rule = 'regular_aggregate_rule'
    bracket_aggregate_rule = 'bracket_aggregate_rule'
    aggregate_rule = 'aggregate_rule'
    direct_name_rule = 'direct_name_rule'
    param_assoc_rule = 'param_assoc_rule'
    call_suffix_rule = 'call_suffix_rule'
    attr_suffix_rule = 'attr_suffix_rule'
    qualified_name_rule = 'qualified_name_rule'
    qual_name_internal_rule = 'qual_name_internal_rule'
    value_sequence_rule = 'value_sequence_rule'
    name_rule = 'name_rule'
    defining_name_rule = 'defining_name_rule'
    direct_name_or_target_name_rule = 'direct_name_or_target_name_rule'
    target_name_rule = 'target_name_rule'
    update_attr_aggregate_rule = 'update_attr_aggregate_rule'
    update_attr_content_rule = 'update_attr_content_rule'
    multidim_array_assoc_rule = 'multidim_array_assoc_rule'
    subtype_name_rule = 'subtype_name_rule'
    static_name_rule = 'static_name_rule'
    primary_rule = 'primary_rule'
    paren_expr_rule = 'paren_expr_rule'
    declare_expr_rule = 'declare_expr_rule'
    factor_rule = 'factor_rule'
    term_rule = 'term_rule'
    unop_term_rule = 'unop_term_rule'
    add_term_rule = 'add_term_rule'
    simple_expr_rule = 'simple_expr_rule'
    boolean_op_rule = 'boolean_op_rule'
    discrete_range_rule = 'discrete_range_rule'
    array_subcomponent_choice_rule = 'array_subcomponent_choice_rule'
    choice_rule = 'choice_rule'
    choice_list_rule = 'choice_list_rule'
    delta_choice_list_rule = 'delta_choice_list_rule'
    rel_op_rule = 'rel_op_rule'
    membership_choice_rule = 'membership_choice_rule'
    membership_choice_list_rule = 'membership_choice_list_rule'
    relation_rule = 'relation_rule'
    expr_rule = 'expr_rule'
    pp_directive_rule = 'pp_directive_rule'
    pp_then_rule = 'pp_then_rule'
    pp_expr_rule = 'pp_expr_rule'
    pp_term_rule = 'pp_term_rule'

    _name = 'GrammarRule'
    _c_to_py = [
        parent_list_rule, protected_type_decl_rule, protected_op_rule, protected_el_rule, protected_def_rule, protected_decl_rule, task_item_rule, task_def_rule, task_type_decl_rule, subtype_decl_rule, interface_type_def_rule, unconstrained_index_rule, array_type_def_rule, discrete_subtype_definition_rule, constraint_list_rule, signed_int_type_def_rule, mod_int_type_def_rule, derived_type_def_rule, composite_constraint_assoc_rule, composite_constraint_rule, digits_constraint_rule, delta_constraint_rule, range_constraint_rule, constraint_rule, discriminant_spec_rule, discr_spec_list_rule, discriminant_part_rule, enum_literal_decl_rule, formal_discrete_type_def_rule, record_def_rule, range_spec_rule, real_type_def_rule, sexpr_or_box_rule, ordinary_fixed_point_def_rule, decimal_fixed_point_def_rule, floating_point_def_rule, record_type_def_rule, access_def_rule, enum_type_def_rule, type_def_rule, variant_rule, anonymous_type_decl_rule, incomplete_type_decl_rule, type_decl_rule, variant_part_rule, component_def_rule, component_item_rule, component_decl_rule, component_list_rule, generic_decl_rule, generic_formal_part_rule, generic_formal_decl_rule, formal_type_decl_rule, formal_subp_decl_rule, renaming_clause_rule, generic_renaming_decl_rule, generic_instantiation_rule, exception_decl_rule, basic_decls_rule, package_renaming_decl_rule, package_decl_rule, basic_decl_rule, object_decl_rule, sub_object_decl_rule, no_type_object_renaming_decl_rule, ext_ret_stmt_object_decl_rule, defining_id_list_rule, number_decl_rule, contract_case_assoc_rule, contract_cases_expr_rule, abstract_state_decl_rule, multi_abstract_state_decl_rule, aspect_assoc_rule, aspect_spec_rule, single_task_decl_rule, overriding_indicator_rule, entry_decl_rule, component_clause_rule, aspect_clause_rule, param_spec_rule, param_specs_rule, subp_spec_rule, expr_fn_rule, null_subp_decl_rule, abstract_subp_decl_rule, subp_renaming_decl_rule, simple_subp_decl_rule, subp_decl_rule, with_clause_rule, context_item_rule, use_clause_rule, use_package_clause_rule, use_type_clause_rule, subtype_indication_rule, discrete_subtype_indication_rule, constrained_subtype_indication_rule, type_expr_rule, anonymous_type_rule, mode_rule, pragma_argument_rule, pragma_rule, subunit_rule, library_unit_body_rule, library_unit_renaming_decl_rule, library_item_rule, compilation_unit_rule, compilation_rule, decl_part_rule, entry_body_rule, protected_body_rule, protected_body_stub_rule, task_body_rule, task_body_stub_rule, package_body_stub_rule, package_body_rule, terminate_alternative_rule, select_stmt_rule, accept_stmt_rule, case_alt_rule, case_stmt_rule, ext_return_stmt_rule, iblock_stmt_rule, block_stmt_rule, while_loop_spec_rule, iloop_stmt_rule, loop_stmt_rule, compound_stmt_rule, elsif_part_rule, if_stmt_rule, raise_stmt_rule, delay_stmt_rule, abort_stmt_rule, body_rule, body_stub_rule, subp_body_stub_rule, recov_decl_part_rule, subp_body_rule, handled_stmts_rule, exception_handler_rule, stmts_rule, label_rule, stmt_rule, call_stmt_rule, simple_stmt_rule, simple_decl_stmt_rule, null_stmt_rule, assignment_stmt_rule, goto_stmt_rule, exit_stmt_rule, return_stmt_rule, requeue_stmt_rule, identifier_rule, char_literal_rule, string_literal_rule, format_string_literal_rule, defining_id_rule, dec_literal_rule, int_literal_rule, num_literal_rule, null_literal_rule, allocator_rule, for_loop_param_spec_rule, quantified_expr_rule, case_expr_rule, case_expr_alt_rule, raise_expr_rule, if_expr_rule, conditional_expr_rule, box_expr_rule, others_designator_rule, iterated_assoc_rule, aggregate_assoc_rule, regular_aggregate_rule, bracket_aggregate_rule, aggregate_rule, direct_name_rule, param_assoc_rule, call_suffix_rule, attr_suffix_rule, qualified_name_rule, qual_name_internal_rule, value_sequence_rule, name_rule, defining_name_rule, direct_name_or_target_name_rule, target_name_rule, update_attr_aggregate_rule, update_attr_content_rule, multidim_array_assoc_rule, subtype_name_rule, static_name_rule, primary_rule, paren_expr_rule, declare_expr_rule, factor_rule, term_rule, unop_term_rule, add_term_rule, simple_expr_rule, boolean_op_rule, discrete_range_rule, array_subcomponent_choice_rule, choice_rule, choice_list_rule, delta_choice_list_rule, rel_op_rule, membership_choice_rule, membership_choice_list_rule, relation_rule, expr_rule, pp_directive_rule, pp_then_rule, pp_expr_rule, pp_term_rule]
    _py_to_c = {name: index for index, name in enumerate(_c_to_py)}
class LookupKind(_Enum):
    """

    """

    recursive = 'recursive'
    flat = 'flat'
    minimal = 'minimal'

    _name = 'LookupKind'
    _c_to_py = [
        recursive, flat, minimal]
    _py_to_c = {name: index for index, name in enumerate(_c_to_py)}
class RefResultKind(_Enum):
    """
    Kind for the result of a cross reference operation.

    * ``no_ref`` is for no reference, it is the null value for this enum.

    * ``precise`` is when the reference result is precise.

    * ``imprecise`` is when there was an error computing the precise result,
      and a result was gotten in an imprecise fashion.

    * ``error`` is for unrecoverable errors (either there is no imprecise path
      for the request you made, or the imprecise path errored out too).
    """

    no_ref = 'no_ref'
    precise = 'precise'
    imprecise = 'imprecise'
    error = 'error'

    _name = 'RefResultKind'
    _c_to_py = [
        no_ref, precise, imprecise, error]
    _py_to_c = {name: index for index, name in enumerate(_c_to_py)}


default_grammar_rule = GrammarRule.compilation_rule


_file_reader = _hashable_c_pointer()
_unit_provider = _hashable_c_pointer()
_event_handler = _hashable_c_pointer()


class _EventHandlerWrapper:
    """
    Wrapper for EventHandler instances, responsible to create the low-level
    event handler value and hold its callbacks.
    """

    __slots__ = (
        "event_handler",
        "c_value",
        "destroy_callback",
        "unit_requested_callback",
        "unit_parsed_callback",
    )

    def __init__(self, event_handler: EventHandler):
        self.event_handler = event_handler

        # Create the C callbacks (wrappers around the _EventHandlerWrapper
        # static method) and keep references to them in "self" so that they
        # survive at least as long as "self".
        self.destroy_callback = _event_handler_destroy_func(
            _EventHandlerWrapper.destroy_func
        )
        self.unit_requested_callback = _event_handler_unit_requested_func(
            _EventHandlerWrapper.unit_requested_func
        )
        self.unit_parsed_callback = _event_handler_unit_parsed_func(
            _EventHandlerWrapper.unit_parsed_func
        )

        # Create the C-level event handler, which keeps a reference to "self"
        # and uses _EventHandlerWrapper's static methods as callbacks.
        self.c_value = _create_event_handler(
            ctypes.py_object(self),
            self.destroy_callback,
            self.unit_requested_callback,
            self.unit_parsed_callback,
        )

    def __del__(self) -> None:
        _dec_ref_event_handler(self.c_value)
        self.c_value = None

    @classmethod
    def create(
        cls,
        event_handler: Opt[EventHandler]
    ) -> Tuple[Opt[_EventHandlerWrapper], Opt[object]]:
        """
        Helper to wrap an EventHandler instance. Return also the C value that
        is created for that event handler. For convenience, just return None
        for both if ``event_handler`` is None.
        """
        if event_handler is None:
            return None, None
        else:
            eh = cls(event_handler)
            return eh, eh.c_value

    @staticmethod
    def destroy_func(self: _EventHandlerWrapper) -> None:
        pass

    @staticmethod
    def unit_requested_func(self: _EventHandlerWrapper,
                            context: object,
                            name: _text,
                            from_unit: object,
                            found: ctypes.c_uint8,
                            is_not_found_error: ctypes.c_uint8) -> None:
        py_context = AnalysisContext._wrap(context)
        py_name = name.contents._wrap()
        py_from_unit = AnalysisUnit._wrap(from_unit)
        try:
            self.event_handler.unit_requested_callback(
                py_context,
                py_name,
                py_from_unit,
                bool(found),
                bool(is_not_found_error),
            )
        except BaseException as exc:
            _log_uncaught_error("EventHandler.unit_requested_callback")

    @staticmethod
    def unit_parsed_func(self: _EventHandlerWrapper,
                         context: object,
                         unit: object,
                         reparsed: ctypes.c_uint8) -> None:
        py_context = AnalysisContext._wrap(context)
        py_unit = AnalysisUnit._wrap(unit)
        try:
            self.event_handler.unit_parsed_callback(
                py_context,
                py_unit,
                bool(reparsed),
            )
        except BaseException as exc:
            _log_uncaught_error("EventHandler.unit_parsed_callback")


def _canonicalize_buffer(buffer: AnyStr,
                         charset: Opt[bytes]) -> Tuple[bytes, Opt[bytes]]:
    """Canonicalize source buffers to be bytes buffers."""
    if isinstance(buffer, str):
        if charset:
            raise TypeError('`charset` must be null when the buffer is'
                            ' Unicode')
        return (buffer.encode('utf-8'), b'utf-8')
    elif not isinstance(buffer, bytes):
        raise TypeError('`buffer` must be a string')
    else:
        return (buffer, charset)


#
# High-level binding
#


class FileReadError(Exception):
    """
    Subprograms may raise this when they cannot open a source file. Note that
    this does *not* concern analysis unit getters, which create diagnostic
    vectors for such errors.
    """
    pass
class BadTypeError(Exception):
    """
    Raised when introspection functions (``Libadalang.Introspection``) are
    provided mismatching types/values.
    """
    pass
class OutOfBoundsError(Exception):
    """
    Raised when introspection functions (``Libadalang.Introspection``) are
    passed an out of bounds index.
    """
    pass
class InvalidInput(Exception):
    """
    Raised by lexing functions (``Libadalang.Lexer``) when the input contains
    an invalid byte sequence.
    """
    pass
class InvalidSymbolError(Exception):
    """
    Exception raise when an invalid symbol is passed to a subprogram.
    """
    pass
class InvalidUnitNameError(Exception):
    """
    Raised when an invalid unit name is provided.
    """
    pass
class NativeException(Exception):
    """
    Exception raised in language bindings when the underlying C API reports an
    unexpected error that occurred in the library.

    This kind of exception is raised for internal errors: they should never
    happen in normal situations and if they are raised at some point, it means
    the library state is potentially corrupted.

    Nevertheless, the library does its best not to crash the program,
    materializing internal errors using this kind of exception.
    """
    pass
class PreconditionFailure(Exception):
    """
    Exception raised when an API is called while its preconditions are not
    satisfied.
    """
    pass
class PropertyError(Exception):
    """
    Exception that is raised when an error occurs while evaluating any AST node
    method whose name starts with ``p_``. This is the only exceptions that such
    functions can raise.
    """
    pass
class TemplateArgsError(Exception):
    """
    Exception raised when the provided arguments for a template don't match
    what the template expects.
    """
    pass
class TemplateFormatError(Exception):
    """
    Exception raised when a template has an invalid syntax, such as badly
    formatted placeholders.
    """
    pass
class TemplateInstantiationError(Exception):
    """
    Exception raised when the instantiation of a template cannot be parsed.
    """
    pass
class StaleReferenceError(Exception):
    """
    Exception raised while trying to access data that was deallocated. This
    happens when one tries to use a node whose unit has been reparsed, for
    instance.
    """
    pass
class SyntaxError(Exception):
    """
    Subprograms may raise this when they try to parse invalid syntax. Note that
    this does *not* concern analysis unit getters, which create diagnostic
    vectors for such errors.
    """
    pass
class UnknownCharset(Exception):
    """
    Raised by lexing functions (``Libadalang.Lexer``) when the input charset is
    not supported.
    """
    pass
class MalformedTreeError(Exception):
    """
    Raised when unparsing functions working on rewritten trees
    (``Libadalang.Rewriting``) are called on malformed trees.
    """
    pass
class ProjectError(Exception):
    """
    Raised when an error occurs while loading a project file.
    """
    pass
class UnsupportedViewError(Exception):
    """
    Raised when creating a project unit provider for an unsupported project
    view (for instance, a view with conflicting aggregated projects).
    """
    pass

_exception_kind_to_type = [
    FileReadError,
    BadTypeError,
    OutOfBoundsError,
    InvalidInput,
    InvalidSymbolError,
    InvalidUnitNameError,
    NativeException,
    PreconditionFailure,
    PropertyError,
    TemplateArgsError,
    TemplateFormatError,
    TemplateInstantiationError,
    StaleReferenceError,
    SyntaxError,
    UnknownCharset,
    MalformedTreeError,
    ProjectError,
    UnsupportedViewError,
]





class EventHandler(Protocol):
    """
    Interface to handle events sent by the analysis context.
    """

    def unit_requested_callback(self,
                                context: AnalysisContext,
                                name: str,
                                from_unit: AnalysisUnit,
                                found: bool,
                                is_not_found_error: bool) -> None:
        """
        Callback that will be called when a unit is requested from the context
        ``Context``.

        ``Name`` is the name of the requested unit.

        ``From`` is the unit from which the unit was requested.

        ``Found`` indicates whether the requested unit was found or not.

        ``Is_Not_Found_Error`` indicates whether the fact that the unit was not
        found is an error or not.

        .. warning:: The interface of this callback is probably subject to
           change, so should be treated as experimental.
        """
        pass

    def unit_parsed_callback(self,
                             context: AnalysisContext,
                             unit: AnalysisUnit,
                             reparsed: bool) -> None:
        """
        Callback that will be called when any unit is parsed from the context
        ``Context``.

        ``Unit`` is the resulting unit.

        ``Reparsed`` indicates whether the unit was reparsed, or whether it was
        the first parse.
        """
        pass


class AnalysisContext:
    """
    This type represents a context for all source analysis. This is the first
    type you need to create to use Libadalang. It will contain the results of
    all analysis, and is the main holder for all the data.

    You can create several analysis contexts if you need to, which enables you,
    for example to:

    * analyze several different projects at the same time;

    * analyze different parts of the same projects in parallel.

    In the current design, contexts always keep all of their analysis units
    allocated. If you need to get this memory released, the only option at your
    disposal is to destroy your analysis context instance.
    """

    __slots__ = ('_c_value', '_unit_provider', '_event_handler_wrapper',
                 '_serial_number', '_unit_cache', '__weakref__')

    _context_cache: weakref.WeakValueDictionary[Any, AnalysisContext] = (
        weakref.WeakValueDictionary()
    )
    """
    Cache for analysis context wrappers. Indexed by analysis context addresses,
    which are known to stay valid forever (and re-used).

    Unlike unit and node caches, this one should contain weak references so
    that analysis contexts (and their units/nodes) can be free'd when user code
    does not reference them anymore.
    """

    def __init__(self,
                 charset: Opt[str] = None,
                 file_reader: Opt[FileReader] = None,
                 unit_provider: Opt[UnitProvider] = None,
                 event_handler: Opt[EventHandler] = None,
                 with_trivia: bool = True,
                 tab_stop: int = 8,
                 *,
                 _c_value: Any = None) -> None:
        """
        Create a new analysis context.

        ``Charset`` will be used as a default charset to decode input sources
        in analysis units. Please see ``GNATCOLL.Iconv`` for several supported
        charsets. Be careful: passing an unsupported charset is not guaranteed
        to raise an error here. If no charset is provided, ``"iso-8859-1"`` is
        the default.

        .. TODO: Passing an unsupported charset here is not guaranteed to raise
           an error right here, but this would be really helpful for users.

        When ``With_Trivia`` is true, the parsed analysis units will contain
        trivias.

        If provided, ``File_Reader`` will be used to fetch the contents of
        source files instead of the default, which is to just read it from the
        filesystem and decode it using the regular charset rules. Note that if
        provided, all parsing APIs that provide a buffer are forbidden, and any
        use of the rewriting API with the returned context is rejected.

        If provided, ``Unit_Provider`` will be used to query the file name that
        corresponds to a unit reference during semantic analysis. If it is
        ``None``, the default one is used instead.

        If provided, ``Event_Handler`` will be notified when various events
        happen.

        ``Tab_Stop`` is a positive number to describe the effect of tabulation
        characters on the column number in source files.
        """

        # Initialize this field in case we raise an exception during
        # construction, so that the destructor can run later on.
        self._c_value = None

        # Create the analysis context if requested, otherwise increase the
        # refcount of the existing context.
        if _c_value is None:
            _charset = _unwrap_charset(charset)
            if not isinstance(tab_stop, int) or tab_stop < 1:
                raise ValueError(
                    'Invalid tab_stop (positive integer expected)')
            c_file_reader = file_reader._c_value if file_reader else None
            c_unit_provider = unit_provider._c_value if unit_provider else None
            self._event_handler_wrapper, c_event_handler = (
                _EventHandlerWrapper.create(event_handler)
            )
            self._c_value = _allocate_analysis_context()
        else:
            self._c_value = _context_incref(_c_value)

        # Register the context in our cache so that wrapping the context in the
        # future always yields the same instance.
        assert self._c_value not in self._context_cache
        self._context_cache[self._c_value] = self

        # Initialize the serial number and the unit cache
        self._serial_number: Opt[int] = None
        self._unit_cache: Dict[str, AnalysisUnit] = {}
        """
        Cache for AnalysisUnit wrappers, indexed by analysis unit addresses,
        which are known to stay valid as long as the context is alive.
        """

        self._check_unit_cache()

        # Now that we have an AnalysisContext wrapper registered, if we just
        # created the analysis context, also initialize it.
        if _c_value is None:
            _initialize_analysis_context(
                self._c_value,
                _charset,
                c_file_reader,
                c_unit_provider,
                c_event_handler,
                with_trivia,
                tab_stop
            )

        # Keep a reference to the unit provider so that it is live at least as
        # long as the analysis context is live.
        self._unit_provider = unit_provider

    def __del__(self) -> None:
        if self._c_value:
            _context_decref(self._c_value)

    def __eq__(self, other: Any) -> bool:
        return self._c_value == other._c_value

    def __hash__(self) -> int:
        return hash(self._c_value)

    def get_from_file(self,
                      filename: AnyStr,
                      charset: Opt[str] = None,
                      reparse: bool = False,
                      rule: str = default_grammar_rule) -> AnalysisUnit:
        """
        Create a new analysis unit for ``Filename`` or return the existing one
        if any. If ``Reparse`` is true and the analysis unit already exists,
        reparse it from ``Filename``.

        ``Rule`` controls which grammar rule is used to parse the unit.

        Use ``Charset`` in order to decode the source. If ``Charset`` is empty
        then use the context's default charset.

        If any failure occurs, such as file opening, decoding, lexing or
        parsing failure, return an analysis unit anyway: errors are described
        as diagnostics of the returned analysis unit.
        """
        _filename = _unwrap_filename(filename)
        _charset = _unwrap_charset(charset)
        c_value = _get_analysis_unit_from_file(self._c_value, _filename,
                                               _charset, reparse,
                                               GrammarRule._unwrap(rule))
        return AnalysisUnit._wrap(c_value)

    def get_from_buffer(self,
                        filename: AnyStr,
                        buffer: AnyStr,
                        charset: Opt[str] = None,
                        reparse: bool = False,
                        rule: str = default_grammar_rule) -> AnalysisUnit:
        """
        Create a new analysis unit for ``Filename`` or return the existing one
        if any. Whether the analysis unit already exists or not, (re)parse it
        from the source code in ``Buffer``.

        ``Rule`` controls which grammar rule is used to parse the unit.

        Use ``Charset`` in order to decode the source. If ``Charset`` is empty
        then use the context's default charset.

        If any failure occurs, such as file opening, decoding, lexing or
        parsing failure, return an analysis unit anyway: errors are described
        as diagnostics of the returned analysis unit.
        """
        _filename = _unwrap_filename(filename)
        _charset = _unwrap_charset(charset)
        _buffer, _charset = _canonicalize_buffer(buffer, _charset)
        c_value = _get_analysis_unit_from_buffer(self._c_value, _filename,
                                                 _charset,
                                                 _buffer, len(_buffer),
                                                 GrammarRule._unwrap(rule))
        return AnalysisUnit._wrap(c_value)

    def get_from_provider(
        self,
        name: AnyStr,
        kind: str,
        charset: Opt[str] = None,
        reparse: bool = False
    ) -> AnalysisUnit:
        """
        Create a new analysis unit for ``Name``/``Kind`` or return the existing
        one if any. If ``Reparse`` is true and the analysis unit already
        exists, reparse it from the on-disk source file.

        The ``Name`` and ``Kind`` arguments are forwarded directly to query the
        context's unit provider and get the filename for the returned unit.
        ``Name`` must be a string, while ``Kind`` must be an
        ``AnalysisUnitKind`` enumeration value. See the documentation of the
        relevant unit provider for their exact semantics.

        Use ``Charset`` in order to decode the source. If ``Charset`` is empty
        then use the context's default charset.

        If the unit name cannot be tuned into a file name, raise an
        ``InvalidUnitNameError`` exception. If any other failure occurs, such
        as file opening, decoding, lexing or parsing failure, return an
        analysis unit anyway: errors are described as diagnostics of the
        returned analysis unit.
        """
        if isinstance(name, bytes):
            text_name = name.decode()
        else:
            text_name = name
        _charset = _unwrap_charset(charset)

        _name = _text._unwrap(text_name)
        _kind = AnalysisUnitKind._unwrap(kind)
        c_value = _get_analysis_unit_from_provider(
            self._c_value, ctypes.byref(_name), _kind, _charset, reparse
        )
        if c_value:
            return AnalysisUnit._wrap(c_value)
        else:
            raise InvalidUnitNameError('Invalid unit name: {} ({})'.format(
                repr(name), kind
            ))

    def discard_errors_in_populate_lexical_env(self,
                                               discard: bool) -> None:
        """
        Debug helper. Set whether ``Property_Error`` exceptions raised in
        ``Populate_Lexical_Env`` should be discarded. They are by default.
        """
        _discard_errors_in_populate_lexical_env(self._c_value, bool(discard))

    class _c_struct(ctypes.Structure):
        _fields_ = [('serial_number', ctypes.c_uint64)]
    _c_type = _hashable_c_pointer(_c_struct)

    @classmethod
    def _wrap(cls, c_value):
        try:
            return cls._context_cache[c_value]
        except KeyError:
            return cls(_c_value=c_value)

    def _check_unit_cache(self):
        """
        If this context has been re-used, invalidate its unit cache.
        """
        serial_number = self._c_value.contents.serial_number
        if self._serial_number != serial_number:
            self._unit_cache = {}
            self._serial_number = serial_number

    
      
    def set_config_pragmas_mapping(
        self,
        global_pragmas: Opt[AnalysisUnit] = None,
        local_pragmas: Opt[Dict[AnalysisUnit, AnalysisUnit]] = None,
    ):
        """
        Assign in ``Context`` configuration pragmas files to analysis units as
        described in ``Global_Pragmas`` (configuration pragmas file that
        applies to all analysis units, or null) and ``Local_Pragmas`` (mapping
        that associates an analysis unit to the local configuration pragmas
        file that applies to it).

        This raises a ``Precondition_Failure`` exception if any analysis unit
        in ``Mapping`` does not belong to ``Context`` or if an analysis unit
        appears twice as a key in ``Mapping``.
        """

        if global_pragmas is None:
            global_c = None
        else:
            assert isinstance(global_pragmas, AnalysisUnit)
            global_c = global_pragmas._c_value

        local_mapping = []
        if local_pragmas is not None:
            for key, value in local_pragmas.items():
                assert isinstance(key, AnalysisUnit)
                assert isinstance(value, AnalysisUnit)
                local_mapping += [key._c_value, value._c_value]
        local_mapping.append(None)

        local_c_type = AnalysisUnit._c_type * len(local_mapping)
        local_c = local_c_type()
        for i, u in enumerate(local_mapping):
            local_c[i] = u

        _set_config_pragmas_mapping(self._c_value, global_c, local_c)

    def set_target_information(self, target_info: TargetInformation) -> None:
        """
        Assign target information to ``self``. This will affect specific bound
        values for types defined in the built in Standard package.
        """
        assert isinstance(target_info, TargetInformation)
        TargetInformation._c_set(self._c_value, target_info._c_value)




class AnalysisUnit:
    """
    This type represents the analysis of a single file.
    """

    __slots__ = ('_c_value', '_context_link', '_cache_version_number',
                 '_node_cache')

    class TokenIterator:
        """
        Iterator over the tokens in an analysis unit.
        """

        def __init__(self, first: Opt[Token]):
            self.first: Opt[Token] = first

        def __iter__(self) -> AnalysisUnit.TokenIterator:
            return self

        def __next__(self) -> Token:
            if not self.first:
                raise StopIteration()
            result = self.first
            self.first = self.first.next
            return result
        next = __next__

    def __init__(self, context: AnalysisContext, c_value: Any) -> None:
        """
        This constructor is an implementation detail, and is not meant to be
        used directly. Please use AnalysisContext.get_from_* methods to create
        analysis unit instances instead.
        """
        self._c_value = c_value

        # Keep a reference on the owning context so that we keep it alive at
        # least as long as this unit is alive.
        self._context_link = context

        # Store this wrapper in caches for later re-use
        assert c_value not in context._unit_cache
        context._unit_cache[c_value] = self

        self._cache_version_number: Opt[int] = None
        """
        Last version number we saw for this analysis unit wrapper. If it's
        different from `self._unit_version`, it means that the unit was
        reparsed: in this case we need to clear the node cache below (see the
        `_check_node_cache` method).
        """

        self._node_cache: Dict[Tuple[Any, Any, Any], AdaNode] = {}
        """
        Cache for all node wrappers in this unit. Indexed by couples:
        (c_value, metadata, rebindings).
        """

        self._check_node_cache()

    def __eq__(self, other: Any) -> bool:
        return self._c_value == other._c_value

    def __hash__(self) -> int:
        return hash(self._c_value)

    @property
    def context(self) -> AnalysisContext:
        """
        Return the context that owns this unit.
        """
        return self._context_link

    def reparse(self,
                buffer: Opt[AnyStr] = None,
                charset: Opt[str] = None) -> None:
        """
        Reparse an analysis unit from a buffer, if provided, or from the
        original file otherwise. If ``Charset`` is empty or ``None``, use the
        last charset successfuly used for this unit, otherwise use it to decode
        the content of the source file.

        If any failure occurs, such as decoding, lexing or parsing failure,
        diagnostic are emitted to explain what happened.
        """
        _charset = _unwrap_charset(charset)
        if buffer is None:
            _unit_reparse_from_file(self._c_value, _charset)
        else:
            _buffer, _charset = _canonicalize_buffer(buffer, _charset)
            _unit_reparse_from_buffer(self._c_value, _charset, _buffer,
                                      len(_buffer))

    def populate_lexical_env(
        self,
            ple_root_index: int,
    ) -> None:
        """
        Create lexical environments for this analysis unit, according to the
        specifications given in the language spec.

        If not done before, it will be automatically called during semantic
        analysis. Calling it before enables one to control where the latency
        occurs.

        Depending on whether errors are discarded (see
        ``Discard_Errors_In_Populate_Lexical_Env``), raise a ``Property_Error``
        on failure.
        """
        if not _unit_populate_lexical_env(
            self._c_value,
                ple_root_index,
        ):
            raise PropertyError()

    @property
    def root(self) -> AdaNode:
        """
        Return the root node for this unit, or ``None`` if there is none.
        """
        result = _Entity_c_type()
        _unit_root(self._c_value, ctypes.byref(result))
        return AdaNode._wrap(result)

    @property
    def first_token(self) -> Opt[Token]:
        """
        Return a reference to the first token scanned in this unit.
        """
        result = Token._c_struct()
        _unit_first_token(self._c_value, ctypes.byref(result))
        return Token._wrap(result)

    @property
    def last_token(self) -> Opt[Token]:
        """
        Return a reference to the last token scanned in this unit.
        """
        result = Token._c_struct()
        _unit_last_token(self._c_value, ctypes.byref(result))
        return Token._wrap(result)

    @property
    def text(self) -> str:
        """
        Return the source buffer associated to this unit.
        """
        if self.first_token:
            assert self.last_token
            return Token.text_range(self.first_token, self.last_token)
        else:
            return ""

    @property
    def token_count(self) -> int:
        """
        Return the number of tokens in this unit.
        """
        return _unit_token_count(self._c_value)

    @property
    def trivia_count(self) -> int:
        """
        Return the number of trivias in this unit. This is 0 for units that
        were parsed with trivia analysis disabled.
        """
        return _unit_trivia_count(self._c_value)

    def lookup_token(self, sloc: Sloc) -> Opt[Token]:
        """
        Look for a token in this unit that contains the given source location.
        If this falls before the first token, return the first token. If this
        falls between two tokens, return the token that appears before. If this
        falls after the last token, return the last token. If there is no token
        in this unit, return no token.
        """
        unit = AnalysisUnit._unwrap(self)
        _sloc = Sloc._c_type._unwrap(sloc)
        result = Token._c_struct()
        _unit_lookup_token(unit, ctypes.byref(_sloc), ctypes.byref(result))
        return Token._wrap(result)

    def _dump_lexical_env(self) -> None:
        """
        Debug helper: output the lexical envs for the given analysis unit.
        """
        unit = AnalysisUnit._unwrap(self)
        _unit_dump_lexical_env(unit)

    def iter_tokens(self) -> AnalysisUnit.TokenIterator:
        """
        Iterator over the tokens in an analysis unit.
        """
        return self.TokenIterator(self.first_token)

    @property
    def filename(self) -> str:
        """
        Return the filename this unit is associated to.
        """
        filename = _unit_filename(self._c_value)
        return _unwrap_str(filename)

    @property
    def diagnostics(self) -> List[Diagnostic]:
        """
        Diagnostics for this unit.
        """
        count = _unit_diagnostic_count(self._c_value)
        result = []
        diag = Diagnostic._c_type()
        for i in range(count):
            success = _unit_diagnostic(self._c_value, i, ctypes.byref(diag))
            assert success
            result.append(diag._wrap())
        return result

    def format_gnu_diagnostic(self, d: Diagnostic) -> str:
        """
        Format a diagnostic in a GNU fashion. See
        <https://www.gnu.org/prep/standards/html_node/Errors.html>.
        """
        sloc = d.sloc_range.start
        prefix = f"{os.path.basename(self.filename)}:"
        if sloc:
            prefix += f"{sloc}:"
        return f"{prefix} {d.message}"

    def __repr__(self) -> str:
        return '<AnalysisUnit {}>'.format(repr(
            os.path.basename(self.filename)
        ))

    class _c_struct(ctypes.Structure):
        _fields_ = [('unit_version', ctypes.c_uint64)]
    _c_type = _hashable_c_pointer(_c_struct)

    @classmethod
    def _wrap(cls, c_value):
        if not c_value:
            return None

        # Invalidate the unit cache if needed, then look for an existing
        # wrapper for this unit.
        context = cls._context(c_value)
        context._check_unit_cache()

        try:
            return context._unit_cache[c_value]
        except KeyError:
            return cls(context, c_value)

    @classmethod
    def _unwrap(cls, value):
        if value is None:
            return value
        elif not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)
        else:
            return value._c_value

    @classmethod
    def _context(cls, c_value) -> AnalysisContext:
        ctx = _unit_context(c_value)
        return AnalysisContext._wrap(ctx)

    @property
    def _unit_version(self) -> int:
        return self._c_value.contents.unit_version

    def _check_node_cache(self) -> None:
        """
        If this unit has been reparsed, invalidate its node cache.
        """
        if self._cache_version_number != self._unit_version:
            self._node_cache = {}
            self._cache_version_number = self._unit_version


class Sloc:
    """
    Location in a source file. Line and column numbers are one-based.
    """

    def __init__(self, line: int, column: int):
        assert line >= 0 and column >= 0
        self.line = line
        self.column = column

    def __bool__(self) -> bool:
        return bool(self.line or self.column)

    def __lt__(self, other: Sloc) -> bool:
        # First compare line numbers...
        if self.line < other.line:
            return True
        elif self.line > other.line:
            return False

        # Past this point, we know that both are on the same line, so now
        # compare column numbers.
        else:
            return self.column < other.column

    def __eq__(self, other: Any) -> bool:
        return self.line == other.line and self.column == other.column

    def __hash__(self) -> int:
        return hash((self.line, self.column))

    def __str__(self) -> str:
        return '{}:{}'.format(self.line, self.column)

    def __repr__(self) -> str:
        return '<Sloc {} at {:#x}>'.format(self, id(self))

    class _c_type(ctypes.Structure):
        _fields_ = [("line", ctypes.c_uint32),
                    ("column", ctypes.c_uint16)]

        def _wrap(self) -> Sloc:
            return Sloc(self.line, self.column)

        @classmethod
        def _unwrap(cls, sloc: Sloc) -> Sloc._c_type:
            return cls(sloc.line, sloc.column)


class SlocRange:
    """
    Location of a span of text in a source file.
    """

    def __init__(self, start: Sloc, end: Sloc):
        self.start = start
        self.end = end

    def __bool__(self) -> bool:
        return bool(self.start or self.end)

    def __lt__(self, other: SlocRange) -> bool:
        raise NotImplementedError('SlocRange comparison not supported')

    def __eq__(self, other: Any) -> bool:
        return self.start == other.start and self.end == other.end

    def __hash__(self) -> int:
        return hash((self.start, self.end))

    def __str__(self) -> str:
        return '{}-{}'.format(self.start, self.end)

    def __repr__(self) -> str:
        return "<SlocRange {}:{}-{}:{}>".format(
            self.start.line, self.start.column,
            self.end.line, self.end.column
        )

    class _c_type(ctypes.Structure):
        _fields_ = [("start", Sloc._c_type),
                    ("end", Sloc._c_type)]

        def _wrap(self) -> SlocRange:
            return SlocRange(self.start._wrap(), self.end._wrap())


class Diagnostic:
    """
    Diagnostic for an analysis unit: cannot open the source file, parsing
    error, ...
    """

    def __init__(self, sloc_range: SlocRange, message: str):
        self.sloc_range = sloc_range
        self.message = message

    @property
    def as_text(self) -> str:
        return (u'{}: {}'.format(self.sloc_range, self.message)
                if self.sloc_range else
                self.message)

    def __str__(self) -> str:
        return self.as_text

    def __repr__(self) -> str:
        return '<Diagnostic {}>'.format(self)


    class _c_type(ctypes.Structure):
        _fields_ = [('sloc_range', SlocRange._c_type),
                    ('message', _text)]

        def _wrap(self) -> Diagnostic:
            return Diagnostic(self.sloc_range._wrap(), self.message._wrap())


class _tdh_c_struct(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint64)]
_tdh_c_type = _hashable_c_pointer(_tdh_c_struct)


class Token:
    """
    Reference to a token in an analysis unit.
    """

    __slots__ = ("_c_value", "_context_version", "_tdh_version")

    class _c_struct(ctypes.Structure):
        _fields_ = [('context',      AnalysisContext._c_type),
                    ('token_data',   _tdh_c_type),
                    ('token_index',  ctypes.c_int),
                    ('trivia_index', ctypes.c_int)]
    _c_type = _hashable_c_pointer(_c_struct)

    def __init__(self, c_value: Any):
        """
        This constructor is an implementation detail and is not meant to be
        used directly.
        """
        self._c_value = c_value
        self._context_version = c_value.context.contents.serial_number
        self._tdh_version = c_value.token_data.contents.version

    @classmethod
    def _wrap(cls, c_value: Any) -> Opt[Token]:
        return cls(c_value) if c_value.token_data else None

    @classmethod
    def _unwrap(cls, value):
        cls._check_token(value)
        return value._c_value

    def _check_stale_reference(self) -> None:
        # First, check that the reference to the context is not stale
        if (
            self._c_value.context.contents.serial_number
            != self._context_version
        ):
            raise StaleReferenceError("owning context was deallocated")

        # The context is valid, so the token data handler is, too: check that
        # no reparsing occured.
        if self._c_value.token_data.contents.version != self._tdh_version:
            raise StaleReferenceError("owning unit was reparsed")

    @staticmethod
    def _check_token(value: Any) -> None:
        if not isinstance(value, Token):
            raise TypeError('invalid token: {}'.format(value))
        value._check_stale_reference()

    def _check_same_unit(self, other: Token) -> None:
        if self._c_value.token_data != other._c_value.token_data:
            raise ValueError('{} and {} come from different analysis units'
                             .format(self, other))

    @property
    def next(self) -> Opt[Token]:
        """
        Return a reference to the next token in the corresponding analysis
        unit.
        """
        self._check_stale_reference()
        result = self._c_struct()
        _token_next(ctypes.byref(self._c_value), ctypes.byref(result))
        return self._wrap(result)

    @property
    def previous(self) -> Opt[Token]:
        """
        Return a reference to the previous token in the corresponding analysis
        unit.
        """
        self._check_stale_reference()
        result = self._c_struct()
        _token_previous(ctypes.byref(self._c_value), ctypes.byref(result))
        return self._wrap(result)

    def range_until(self, other: Token) -> Iterator[Token]:
        """
        Return an iterator on the list of tokens that spans between ``self``
        and ``other`` (included). This returns an empty list if the first token
        appears after the other one in the source code. Raise a ``ValueError``
        if both tokens come from different analysis units.
        """
        self._check_stale_reference()
        self._check_token(other)
        self._check_same_unit(other)

        # Keep the generator as a nested function so that the above checks are
        # executed when the generator is created, instead of only when its
        # first item is requested.
        #
        # Note that, because the execution of a generator stops and resumes,
        # the tokens may become stale after it resumes: check for stale
        # references at starting and resuming time.
        def generator() -> Iterator[Token]:
            self._check_stale_reference()
            if other < self:
                return

            yield self
            current = self
            while current < other:
                next = current.next
                assert next is not None
                yield next
                self._check_stale_reference()
                current = next
        return generator()

    def is_equivalent(self, other: Token) -> bool:
        """
        Return whether ``L`` and ``R`` are structurally equivalent tokens. This
        means that their position in the stream won't be taken into account,
        only the kind and text of the token.
        """
        self._check_stale_reference()
        self._check_token(other)
        return bool(_token_is_equivalent(
            ctypes.byref(self._c_value), ctypes.byref(other._c_value))
        )

    @property
    def kind(self) -> str:
        """
        Kind for this token.
        """
        self._check_stale_reference()
        kind = _token_get_kind(self._c_value)
        name = _token_kind_name(kind)
        # The _token_kind_name wrapper is already supposed to handle exceptions
        # so this should always return a non-null value.
        assert name
        return _unwrap_str(name)

    @property
    def is_trivia(self) -> bool:
        """
        Return whether this token is a trivia. If it's not, it's a regular
        token.
        """
        self._check_stale_reference()
        return self._c_value.trivia_index != 0

    @property
    def index(self) -> int:
        """
        Zero-based index for this token/trivia. Tokens and trivias get their
        own index space.
        """
        self._check_stale_reference()
        return (self._c_value.token_index - 1
                if self._c_value.trivia_index == 0 else
                self._c_value.trivia_index - 1)

    @property
    def text(self) -> str:
        """
        Return the text of the given token.
        """
        return self.text_range(self, self)

    @classmethod
    def text_range(cls, first: Token, last: Token) -> str:
        """
        Compute the source buffer slice corresponding to the text that spans
        between the ``First`` and ``Last`` tokens (both included). This yields
        an empty slice if ``Last`` actually appears before ``First``.

        This raises a ``ValueError`` if ``First`` and ``Last`` don't belong to
        the same analysis unit.
        """
        cls._check_token(first)
        cls._check_token(last)
        first._check_same_unit(last)
        result = _text()
        success = _token_range_text(
            ctypes.byref(first._c_value),
            ctypes.byref(last._c_value),
            ctypes.byref(result),
        )
        assert success
        return result._wrap() or u''

    @property
    def sloc_range(self) -> SlocRange:
        """
        Return the source location range of the given token.
        """
        self._check_stale_reference()
        result = SlocRange._c_type()
        _token_sloc_range(ctypes.byref(self._c_value), ctypes.byref(result))
        return result._wrap()

    def __eq__(self, other: Any) -> bool:
        """
        Return whether the two tokens refer to the same token in the same unit.

        Note that this does not actually compares the token data.
        """
        return (isinstance(other, Token)
                and self._identity_tuple == other._identity_tuple)

    def __hash__(self) -> int:
        return hash(self._identity_tuple)

    def __repr__(self) -> str:
        self._check_stale_reference()
        return '<Token {}{} at {}>'.format(
            self.kind,
            ' {}'.format(repr(self.text)) if self.text else '',
            self.sloc_range
        )

    def __lt__(self, other: Opt[Token]):
        """
        Consider that None comes before all tokens. Then, sort by unit, token
        index, and trivia index.
        """
        self._check_stale_reference()

        # None always comes first
        if other is None:
            return False

        self._check_token(other)
        self._check_same_unit(other)
        return self._identity_tuple < other._identity_tuple

    def __le__(self, other: Opt[Token]) -> bool:
        return self == other or self < other

    def __gt__(self, other: Opt[Token]) -> bool:
        return not (self <= other)

    def __ge__(self, other: Opt[Token]) -> bool:
        return not (self < other)

    def to_data(self) -> dict:
        """
        Return a dict representation of this Token.
        """
        return {"kind": "Token", "token_kind": self.kind, "text": self.text}

    @property
    def _identity_tuple(self) -> Tuple[Any, int, int]:
        """
        Return a tuple that return a tuple that contains "identity" information
        for this token. Think of it as a database primary key.

        This property is for internal use only.
        """
        return (
            self._c_value.token_data,
            self._c_value.token_index,
            self._c_value.trivia_index
        )


class FileReader:
    """
    Interface to override how source files are fetched and decoded.
    """

    def __init__(self, c_value: Any):
        """
        This constructor is an implementation detail, and is not meant to be
        used directly.
        """
        self._c_value = c_value

    def __del__(self) -> None:
        _dec_ref_file_reader(self._c_value)


      
    class LineMode(_Enum):
        """
        Determine how the preprocessor treats directives and disabled lines in
        the output.

        ``delete_lines``

          Just delete these lines: this breaks line number correspondance
          between the original source and the preprocessed one. This
          corresponds to GNATprep's default mode.

        ``blank_lines``

          Replace these lines with empty lines. This corresponds to GNATprep's
          ``-b`` option.

        ``comment_lines``

          Preserve these lines and emit a ``--!`` comment marker in front of
          them. This corresponds to GNATprep's ``-c`` option.
        """

        delete_lines = "delete_lines"
        blank_lines = "blank_lines"
        comment_lines = "comment_lines"

        _name = "LineMode"
        _c_to_py = [delete_lines, blank_lines, comment_lines]
        _py_to_c = {name: index for index, name in enumerate(_c_to_py)}

    @classmethod
    def create_preprocessor_from_file(
        cls,
        filename: str,
        path: List[str],
        line_mode: Optional[FileReader.LineMode]
    ) -> FileReader:
        """
        Load the preprocessor data file at ``filename``, using directory names
        in ``path`` to look for for it and the definition files it references.

        If ``line_mode`` is passed, use it to force the line mode for source
        files on which the preprocessor is enabled.  Forcing the line mode is
        often needed as the default is to remove lines that contain
        preprocessor directives and disabled code, which breaks the line number
        correspondence between original source code and preprocessed one.
        Forcing to ``blank_lines`` or ``comment_lines`` preserves this
        correspondence.

        Return a file reader that preprocesses sources accordingly.
        """

        # Create an array of C strings to hold the path directories
        c_dirs = [ctypes.c_char_p(_unwrap_filename(d)) for d in path]
        c_path_data = (ctypes.c_char_p * len(c_dirs))()
        for i, d in enumerate(c_dirs):
            c_path_data[i] = d

        # Create the pointer to this array, with the expected type according to
        # ctypes.
        c_path_type = ctypes.POINTER(ctypes.c_char_p)
        c_path = ctypes.cast(ctypes.byref(c_path_data), c_path_type)

        # Pass the line mode to force, if any
        if line_mode is not None:
            lm = ctypes.c_int(FileReader.LineMode._unwrap(line_mode))
            lm_ref = ctypes.byref(lm)
        else:
            lm_ref = ctypes.POINTER(ctypes.c_int)()

        # We can now create the file reader itself
        c_value = _create_preprocessor_from_file(
            _unwrap_filename(filename), c_path, len(c_dirs), lm_ref
        )
        return cls(c_value)




class UnitProvider:
    """
    Interface to fetch analysis units from a name and a unit kind.

    The unit provider mechanism provides an abstraction which assumes that to
    any couple (unit name, unit kind) we can associate at most one source file.
    This means that several couples can be associated to the same source file,
    but on the other hand, only one one source file can be associated to a
    couple.

    This is used to make the semantic analysis able to switch from one analysis
    units to another.

    See the documentation of each unit provider for the exact semantics of the
    unit name/kind information.
    """

    def __init__(self, c_value: Any):
        """
        This constructor is an implementation detail, and is not meant to be
        used directly.
        """
        self._c_value = c_value

    def __del__(self) -> None:
        _dec_ref_unit_provider(self._c_value)


      
    @classmethod
    def for_project(cls, project_file, project=None, scenario_vars=None,
                    target=None, runtime=None):
        """
        Load the project file at ``Project_File`` and return a unit provider
        that uses it.

        If ``Project`` is passed, use it to provide units, otherwise, try use
        the whole project tree.

        As unit providers must guarantee that there exists at most one source
        file for each couple (unit name, unit kind), aggregate projects that
        contains several conflicting units are not supported: trying to load
        one will yield an error (see below).

        If provided, ``Scenario_Vars`` must be a dict with key strings and key
        values to describe the set of scenario variables for this project.

        In order to load the given project with non-default target and
        runtimes, pass these as strings to the ``target`` and ``runtime``
        arguments.

        If the requested project is invalid (error while opening the file,
        error while analysing its syntax, ...), or if it is an unsupported
        aggregate project, this raises an ``InvalidProjectError`` exception.
        """

        prj = GPRProject(project_file, scenario_vars, target, runtime,
                         print_errors=False)
        return prj.create_unit_provider(project)

    @classmethod
    def auto(cls, input_files, charset=None):
        """
        Return a unit provider that knows which compilation units are to be
        found in the given list of source files.

        This knowledge is built trying to parse all given input files as Ada
        source files and listing the compilation units found there. Files that
        cannot be parsed properly are discarded. If two compilation units are
        found for the same unit, the first that is found in the given input
        files is taken and the other ones are discarded.

        Source files are decoded using the given charset. If it is ``None``,
        the default charset (ISO-8859-1) is used.

        .. TODO: Find a way to report discarded source files/compilation units.
        """

        # Create a NULL-terminated array of strings
        c_strings = [
            ctypes.c_char_p(_coerce_bytes('input_files', f,
                                          'a list of bytes strings'))
            for f in input_files
        ]
        c_array_type = ctypes.c_char_p * (len(input_files) + 1)
        c_array = c_array_type()
        for i, c_str in enumerate(c_strings):
            c_array[i] = c_str
        c_array[-1] = None

        c_array_ptr = ctypes.pointer(c_array)
        input_files_arg = ctypes.cast(c_array_ptr,
                                      ctypes.POINTER(ctypes.c_char_p))

        c_charset = _unwrap_charset(charset)

        c_value = _create_auto_provider(input_files_arg, c_charset)
        return cls(c_value)




class AdaNode:
    """
    Root node class for the Ada syntax tree.

    Derived nodes: :py:class:`AbortNode`, :py:class:`AbstractNode`,
    :py:class:`AdaList`, :py:class:`AliasedNode`, :py:class:`AllNode`,
    :py:class:`ArrayIndices`, :py:class:`AspectAssoc`,
    :py:class:`AspectClause`, :py:class:`AspectSpec`, :py:class:`BaseAssoc`,
    :py:class:`BaseFormalParamHolder`, :py:class:`BaseRecordDef`,
    :py:class:`BasicAssoc`, :py:class:`BasicDecl`,
    :py:class:`CaseStmtAlternative`, :py:class:`CompilationUnit`,
    :py:class:`ComponentClause`, :py:class:`ComponentDef`,
    :py:class:`ConstantNode`, :py:class:`Constraint`,
    :py:class:`DeclarativePart`, :py:class:`ElsePart`,
    :py:class:`ElsifExprPart`, :py:class:`ElsifStmtPart`, :py:class:`Expr`,
    :py:class:`ForLoopIterFilter`, :py:class:`FormatStringChunk`,
    :py:class:`FormatStringTokNode`, :py:class:`HandledStmts`,
    :py:class:`InterfaceKind`, :py:class:`IterType`, :py:class:`LibraryItem`,
    :py:class:`LimitedNode`, :py:class:`LoopSpec`, :py:class:`Mode`,
    :py:class:`MultiAbstractStateDecl`, :py:class:`NotNull`,
    :py:class:`NullComponentDecl`, :py:class:`OthersDesignator`,
    :py:class:`OverridingNode`, :py:class:`Params`,
    :py:class:`ParenAbstractStateDecl`, :py:class:`PpDirective`,
    :py:class:`PpThenKw`, :py:class:`PragmaNode`, :py:class:`PrivateNode`,
    :py:class:`ProtectedDef`, :py:class:`ProtectedNode`,
    :py:class:`Quantifier`, :py:class:`RangeSpec`, :py:class:`RenamingClause`,
    :py:class:`ReverseNode`, :py:class:`SelectWhenPart`, :py:class:`Stmt`,
    :py:class:`SubpKind`, :py:class:`Subunit`, :py:class:`SynchronizedNode`,
    :py:class:`TaggedNode`, :py:class:`TaskDef`, :py:class:`ThenAbortPart`,
    :py:class:`TypeAttributesRepository`, :py:class:`TypeDef`,
    :py:class:`TypeExpr`, :py:class:`UnconstrainedArrayIndex`,
    :py:class:`UntilNode`, :py:class:`UseClause`, :py:class:`ValueSequence`,
    :py:class:`VariantPart`, :py:class:`Variant`, :py:class:`WithClause`,
    :py:class:`WithPrivate`
    """

    is_list_type = False
    __slots__ = ('_unprotected_c_value', '_node_c_value', '_metadata',
                 '_rebindings', '_unprotected_getitem_cache', '_unit',
                 '_unit_version', '_rebindings_version')

    _kind_name: str
    _field_names: Tuple[str, ...]

    
    

    
    @property
    def parent(
        self
    ) -> AdaNode:
        """
        Return the syntactic parent for this node. Return null for the root
        node.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_parent)
        result = AdaNode._wrap(c_result)


        return result
    
    def parents(
        self, with_self: bool = True
    ) -> List[AdaNode]:
        """
        Return an array that contains the lexical parents, this node included
        iff ``with_self`` is True. Nearer parents are first in the list.
        """
        

        

        unwrapped_with_self = bool(with_self)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _ada_node_parents, unwrapped_with_self)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def children(
        self
    ) -> List[AdaNode]:
        """
        Return an array that contains the direct lexical children.

        .. warning:: This constructs a whole array every-time you call it, and
           as such is less efficient than calling the ``Child`` built-in.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _ada_node_children)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def token_start(
        self
    ) -> Opt[Token]:
        """
        Return the first token used to parse this node.
        """
        

        


        
        c_result = self._eval_field(Token._c_struct(), _ada_node_token_start)
        result = Token._wrap(c_result)


        return result
    
    @property
    def token_end(
        self
    ) -> Opt[Token]:
        """
        Return the last token used to parse this node.
        """
        

        


        
        c_result = self._eval_field(Token._c_struct(), _ada_node_token_end)
        result = Token._wrap(c_result)


        return result
    
    @property
    def child_index(
        self
    ) -> int:
        """
        Return the 0-based index for Node in its parent's children.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_int(), _ada_node_child_index)
        result = c_result.value


        return result
    
    @property
    def previous_sibling(
        self
    ) -> AdaNode:
        """
        Return the node's previous sibling, or null if there is no such
        sibling.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_previous_sibling)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def next_sibling(
        self
    ) -> AdaNode:
        """
        Return the node's next sibling, or null if there is no such sibling.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_next_sibling)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def unit(
        self
    ) -> AnalysisUnit:
        """
        Return the analysis unit owning this node.
        """
        

        


        
        c_result = self._eval_field(AnalysisUnit._c_type(), _ada_node_unit)
        result = AnalysisUnit._wrap(c_result)


        return result
    
    @property
    def is_ghost(
        self
    ) -> bool:
        """
        Return whether the node is a ghost.

        Unlike regular nodes, ghost nodes cover no token in the input source:
        they are logically located instead between two tokens. Both the
        ``token_start`` and the ``token_end`` of all ghost nodes is the token
        right after this logical position.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _ada_node_is_ghost)
        result = bool(c_result.value)


        return result
    
    @property
    def full_sloc_image(
        self
    ) -> str:
        """
        Return a string containing the filename + the sloc in GNU conformant
        format. Useful to create diagnostics from a node.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _ada_node_full_sloc_image)
        result = _String.wrap(c_result)


        return result
    
    def completion_item_kind_to_int(
        self, kind: str
    ) -> int:
        """
        Convert a CompletionItemKind enum to its corresponding integer value.
        """
        

        

        unwrapped_kind = CompletionItemKind._unwrap(kind)

        
        c_result = self._eval_field(ctypes.c_int(), _ada_node_completion_item_kind_to_int, unwrapped_kind)
        result = c_result.value


        return result
    
    @property
    def p_declarative_scope(
        self
    ) -> DeclarativePart:
        """
        Return the scope of definition of this basic declaration.
        """
        

        

        result = self._eval_astnode_field(_ada_node_p_declarative_scope)



        return result
    
    @property
    def p_enclosing_compilation_unit(
        self
    ) -> CompilationUnit:
        """
        Return the compilation unit containing this node.

        .. note:: This returns the :py:class:`CompilationUnit` node, which is
           different from the ``AnalysisUnit``. In particular, an analysis unit
           can contain multiple compilation units.
        """
        

        

        result = self._eval_astnode_field(_ada_node_p_enclosing_compilation_unit)



        return result
    
    @property
    def p_get_uninstantiated_node(
        self
    ) -> AdaNode:
        """
        Assuming this node comes from an instantiated generic declaration,
        return its non-instantiated counterpart lying in the generic
        declaration.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_get_uninstantiated_node)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_complete(
        self
    ) -> CompletionItemIterator:
        """
        Return possible completions at this point in the file.
        """
        

        


        
        c_result = self._eval_field(CompletionItemIterator._c_type(), _ada_node_p_complete)
        result = CompletionItemIterator._wrap(c_result)


        return result
    
    @property
    def p_valid_keywords(
        self
    ) -> List[str]:
        """
        Return the list of keywords that are valid at this point in the file.

        .. note:: This is work in progress. It will return all keywords for
           now, without looking at the context.
        """
        

        


        
        c_result = self._eval_field(_UnboundedTextTypeArrayConverter.c_type(), _ada_node_p_valid_keywords)
        result = _UnboundedTextTypeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_generic_instantiations(
        self
    ) -> List[GenericInstantiation]:
        """
        Return the potentially empty list of generic package/subprogram
        instantiations that led to the creation of this entity. Outer-most
        instantiations appear last.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _ada_node_p_generic_instantiations)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_semantic_parent(
        self
    ) -> AdaNode:
        """
        Return the semantic parent for this node, if applicable, null
        otherwise.

        .. note:: A node lying outside of a library item's declaration or
           subunit's body does not have a parent environment, meaning that this
           property will return null.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_semantic_parent)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_parent_basic_decl(
        self
    ) -> BasicDecl:
        """
        Return the parent basic decl for this node, if applicable, null
        otherwise.

        .. note:: If the parent BasicDecl of the given node is a generic
           declaration, this call will return the instantiation from which the
           node was retrieved instead, if any. This also applies to bodies of
           generic declarations.

        .. note:: When called on a subunit's body, this property will return
           its corresponding body stub.

        .. note:: When called on a node lying outside of a library item's
           declaration or subunit's body this property will return null.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_parent_basic_decl)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_has_spark_mode_on(
        self
    ) -> bool:
        """
        Returns whether this subprogram has explicitly been set as having
        ``Spark_Mode`` to ``On``, directly or indirectly.

        Doesn't include subprograms that can be inferred by GNATprove as being
        SPARK.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _ada_node_p_has_spark_mode_on)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_subject_to_proof(
        self
    ) -> bool:
        """
        Returns whether this subprogram body is subject to proof in the context
        of the SPARK/GNATprove tools.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _ada_node_p_is_subject_to_proof)
        result = bool(c_result.value)


        return result
    
    def p_filter_is_imported_by(
        self, units: List[AnalysisUnit], transitive: bool
    ) -> List[AnalysisUnit]:
        """
        Filters out among the list of given units those that cannot refer to
        the unit in which this node lies. If transitive is True, the whole
        transitive closure of imports will be used to find a reference to the
        unit of this node.
        """
        

        

        unwrapped_units = _AnalysisUnitArrayConverter.unwrap(units)
        unwrapped_transitive = bool(transitive)

        
        c_result = self._eval_field(_AnalysisUnitArrayConverter.c_type(), _ada_node_p_filter_is_imported_by, unwrapped_units.c_value, unwrapped_transitive)
        result = _AnalysisUnitArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_resolve_names(
        self
    ) -> bool:
        """
        This will resolve names for this node. If the operation is successful,
        then type_var and ref_var will be bound on appropriate subnodes of the
        statement.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _ada_node_p_resolve_names)
        result = bool(c_result.value)


        return result
    
    @property
    def p_nameres_diagnostics(
        self
    ) -> List[SolverDiagnostic]:
        """
        If name resolution on this xref entry point fails, this returns all the
        diagnostics that were produced while resolving it.
        """
        

        


        
        c_result = self._eval_field(_SolverDiagnosticArrayConverter.c_type(), _ada_node_p_nameres_diagnostics)
        result = _SolverDiagnosticArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_standard_unit(
        self
    ) -> AnalysisUnit:
        """
        Static method. Return the analysis unit corresponding to the Standard
        package.
        """
        

        


        
        c_result = self._eval_field(AnalysisUnit._c_type(), _ada_node_p_standard_unit)
        result = AnalysisUnit._wrap(c_result)


        return result
    
    def p_is_keyword(
        self, token: Opt[Token], language_version: str
    ) -> bool:
        """
        Static method. Return whether the given token is considered a keyword
        in the given version of Ada. Supported values for the language version
        argument are: "Ada_83", "Ada_95", "Ada_2005", "Ada_2012", "Ada_2022".
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_token = Token._unwrap(token)
        unwrapped_language_version = _symbol_type.unwrap(language_version, _context)

        
        c_result = self._eval_field(ctypes.c_uint8(), _ada_node_p_is_keyword, ctypes.byref(unwrapped_token), unwrapped_language_version)
        result = bool(c_result.value)


        return result
    
    def p_std_entity(
        self, sym: str
    ) -> AdaNode:
        """
        Static property. Return an entity from the standard package with name
        ``sym``.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_sym = _symbol_type.unwrap(sym, _context)

        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_std_entity, unwrapped_sym)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_bool_type(
        self
    ) -> BaseTypeDecl:
        """
        Static method. Return the standard Boolean type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_bool_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_int_type(
        self
    ) -> BaseTypeDecl:
        """
        Static method. Return the standard Integer type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_int_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_universal_int_type(
        self
    ) -> BaseTypeDecl:
        """
        Static method. Return the standard Universal Integer type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_universal_int_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_universal_real_type(
        self
    ) -> BaseTypeDecl:
        """
        Static method. Return the standard Universal Real type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_universal_real_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_std_char_type(
        self
    ) -> BaseTypeDecl:
        """
        Static method. Return the standard Character type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_std_char_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_std_wide_char_type(
        self
    ) -> BaseTypeDecl:
        """
        Static method. Return the standard Wide_Character type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_std_wide_char_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_std_wide_wide_char_type(
        self
    ) -> BaseTypeDecl:
        """
        Static method. Return the standard Wide_Wide_Character type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_std_wide_wide_char_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_std_string_type(
        self
    ) -> BaseTypeDecl:
        """
        Static method. Return the standard String type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_std_string_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_std_wide_string_type(
        self
    ) -> BaseTypeDecl:
        """
        Static method. Return the standard Wide_String type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_std_wide_string_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_std_wide_wide_string_type(
        self
    ) -> BaseTypeDecl:
        """
        Static method. Return the standard Wide_Wide_String type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_std_wide_wide_string_type)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_top_level_decl(
        self, unit: AnalysisUnit
    ) -> BasicDecl:
        """
        Static method. Get the top-level decl in ``unit``.  This is the body of
        a Subunit, or the item of a ``LibraryItem``.
        """
        

        

        unwrapped_unit = AnalysisUnit._unwrap(unit)

        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_top_level_decl, unwrapped_unit)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_choice_match(
        self, value: int
    ) -> bool:
        """
        Assuming that self is a choice expression (such as what can appear in
        an alternative of a case statement or in the RHS of a membership
        expression, this property returns whether the given value satisfies it.

        .. attention:: This is an experimental feature, so even if it is
           exposed to allow experiments, it is totally unsupported and the API
           and behavior are very likely to change in the future.
        """
        

        

        unwrapped_value = _big_integer.unwrap(value)

        
        c_result = self._eval_field(ctypes.c_uint8(), _ada_node_p_choice_match, unwrapped_value.c_value)
        result = bool(c_result.value)


        return result
    
    def p_gnat_xref(
        self, imprecise_fallback: bool = False
    ) -> DefiningName:
        """
        Return a cross reference from this name to a defining identifier,
        trying to mimic GNAT's xrefs as much as possible.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _ada_node_p_gnat_xref, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_spark_mode_aspect(
        self
    ) -> Aspect:
        """
        Helper for the ``has_spark_mode_on`` and ``is_subject_to_proof``
        properties.

        This property will get the applicable aspect defining the SPARK_Mode
        for the given node, recursing syntactically and taking into account
        configuration files.

        This only implements the base logic for recursing up the tree: nodes
        that need a specific logic must override it. See for example
        ``BasicDecl.spark_mode_aspect``.
        """
        

        


        
        c_result = self._eval_field(Aspect._c_type(), _ada_node_p_spark_mode_aspect)
        result = Aspect._wrap(c_result)


        return result
    
    @property
    def p_xref_entry_point(
        self
    ) -> bool:
        """
        Designates entities that are entry point for the xref solving
        infrastructure. If this returns true, then resolve_names can be called
        on it.

        .. note:: For convenience, and unlike what is defined in the ARM wrt.
           complete contexts for name resolution, ``xref_entry_points`` can be
           nested.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _ada_node_p_xref_entry_point)
        result = bool(c_result.value)


        return result
    
    @property
    def p_matching_with_use_clause(
        self
    ) -> bool:
        """
        Return whether this node is a ``UsePackageClause`` that follows a
        ``WithClause`` for the same unit.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _ada_node_p_matching_with_use_clause)
        result = bool(c_result.value)


        return result

    _field_names = () + (
    )




    def __init__(self, c_value: Any, node_c_value: Any, rebindings: Any):
        """
        This constructor is an implementation detail, and is not meant to be
        used directly. For now, the creation of AST nodes can happen only as
        part of the parsing of an analysis unit.
        """

        self._unprotected_c_value = c_value

        # Access to these fields is unprotected from stale references, but it
        # is supposed to be used only in _id_tuple, which itself should not be
        # used outside of hashing/equality use cases.
        self._node_c_value = node_c_value
        self._rebindings = rebindings

        self._unprotected_getitem_cache: Dict[int,
                                              Opt[AdaNode]] = {}
        """
        Cache for the __getitem__ override.
        """

        # Information to check before accessing node data that it is still
        # valid.
        self._unit = self._fetch_unit(c_value)
        self._unit_version = self._unit._unit_version
        self._rebindings_version = (
            rebindings.contents.version if rebindings else None
        )

    def _check_stale_reference(self) -> None:
        # We have a reference to the owning unit, so there is no need to
        # check that the unit and the context are still valid. Just check that
        # the unit has not been reparsed.
        if self._unit._unit_version != self._unit_version:
            raise StaleReferenceError("unit was reparsed")

        # Also check that the rebindings are still valid
        if (
            self._rebindings
            and self._rebindings.contents.version != self._rebindings_version
        ):
            raise StaleReferenceError("related unit was reparsed")

    @property
    def _c_value(self) -> Any:
        self._check_stale_reference()
        return self._unprotected_c_value

    @property
    def _getitem_cache(self) -> Dict[int, Opt[AdaNode]]:
        self._check_stale_reference()
        return self._unprotected_getitem_cache

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, AdaNode)
            and bool(
                _node_is_equivalent(
                    ctypes.byref(self._unsafe_unwrap),
                    ctypes.byref(other._unsafe_unwrap)
                )
            )
        )

    def __ne__(self, other: Any) -> bool:
        return not (self == other)

    def __hash__(self) -> int:
        return _node_hash(ctypes.byref(self._unsafe_unwrap))

    @property
    def kind_name(self) -> str:
        """
        Return the kind of this node.
        """
        return self._kind_name

    @property
    def is_token_node(self) -> bool:
        """
        Return whether this node is a node that contains only a single token.
        """
        node = self._unwrap(self)
        return bool(_node_is_token_node(ctypes.byref(node)))

    @property
    def is_synthetic(self) -> bool:
        """
        Return whether this node is synthetic.
        """
        node = self._unwrap(self)
        return bool(_node_is_synthetic(ctypes.byref(node)))

    @property
    def sloc_range(self) -> SlocRange:
        """
        Return the spanning source location range for this node.

        Note that this returns the sloc of the parent for synthetic nodes.
        """
        node = self._unwrap(self)
        result = SlocRange._c_type()
        _node_sloc_range(ctypes.byref(node), ctypes.byref(result))
        return result._wrap()

    @property
    def text(self) -> str:
        """
        Return the source buffer slice corresponding to the text that spans
        between the first and the last tokens of this node.

        Note that this returns the empty string for synthetic nodes.
        """
        node = self._unwrap(self)
        result = _text()
        _node_text(ctypes.byref(node), ctypes.byref(result))
        return result._wrap()

    @property
    def image(self) -> str:
        """
        Return a representation of this node as a string.
        """
        c_node = self._unwrap(self)
        c_result = _text()
        _node_image(ctypes.byref(c_node), ctypes.byref(c_result))
        return c_result._wrap()

    def lookup(self, sloc: Sloc) -> Opt[AdaNode]:
        """
        Return the bottom-most node from in ``Node`` and its children which
        contains ``Sloc``, or ``None`` if there is none.
        """
        node = self._unwrap(self)
        c_sloc = Sloc._c_type._unwrap(sloc)
        result = _Entity_c_type()
        _lookup_in_node(ctypes.byref(node), ctypes.byref(c_sloc),
                        ctypes.byref(result))
        return AdaNode._wrap(result)

    def __bool__(self) -> bool:
        """
        Return always True so that checking a node against None can be done as
        simply as:

        .. code::

           if node:
               ...
        """
        return True

    def __iter__(self) -> Iterator[Opt[AdaNode]]:
        """
        Return an iterator on the children of this node.
        """
        for i in range(len(self)):
            yield self[i]

    def __len__(self) -> int:
        """
        Return the number of AdaNode children this node has.
        """
        node = self._unwrap(self)
        return _node_children_count(ctypes.byref(node))

    def __getitem__(self, key: int) -> Opt[AdaNode]:
        """
        Return the Nth AdaNode child this node has.

        This handles negative indexes the same way Python lists do. Raise an
        IndexError if "key" is out of range.
        """
        if not isinstance(key, int):
            msg = ('AdaNode children are integer-indexed'
                   ' (got {})').format(type(key))
            raise TypeError(msg)

        if key < 0:
            key += len(self)

        if key in self._getitem_cache:
            return self._getitem_cache[key]

        node = self._unwrap(self)
        result_struct = _Entity_c_type()
        success = _node_child(
            ctypes.byref(node), key, ctypes.byref(result_struct)
        )
        if not success:
            raise IndexError('child index out of range')
        else:
            result = AdaNode._wrap(result_struct)
            self._getitem_cache[key] = result
            return result

    def iter_fields(self) -> Iterator[Tuple[str, Opt[AdaNode]]]:
        """
        Iterate through all the fields this node contains.

        Return an iterator that yields (name, value) couples for all abstract
        fields in this node. If "self" is a list, field names will be
        "item_{n}" with "n" being the index.
        """
        if self.is_list_type:
            for i, value in enumerate(self):
                yield ('item_{}'.format(i), value)
        else:
            for field_name in self._field_names:
                yield (field_name, getattr(self, '{}'.format(field_name)))

    def dump_str(self) -> str:
        """
        Dump the sub-tree to a string in a human-readable format.
        """
        output = io.StringIO()
        self.dump(file=output)
        ret = output.getvalue()
        output.close()
        return ret

    def dump(self, indent: str = '', file: IO[str] = sys.stdout) -> None:
        """
        Dump the sub-tree in a human-readable format on the given file.

        :param str indent: Prefix printed on each line during the dump.

        :param file file: File in which the dump must occur.
        """

        def print_node(name, value):
            if isinstance(value, AdaNode):
                print('{}{}:'.format(indent, name), file=file)
                value.dump(indent + '  ', file)
            else:
                print('{}{}: {}'.format(indent, name, value), file=file)

        node_repr = str(self)[1:-1]
        print('{}{}{}'.format(
            indent, node_repr,
            ': {}'.format(self.text) if self.is_token_node else ''
        ), file=file)
        indent = indent + '|'
        if self.is_list_type:
            for i, value in enumerate(self):
                print_node("item_{}".format(i), value)
        else:
            for name, value in self.iter_fields():
                print_node(name, value)

    def findall(
        self,
        ast_type_or_pred: Union[Type[AdaNode],
                                Callable[[AdaNode], bool]],
        **kwargs: Any
    ) -> List[AdaNode]:
        """
        Helper for finditer that will return all results as a list. See
        finditer's documentation for more details.
        """
        return list(self.finditer(ast_type_or_pred, **kwargs))

    def find(
        self,
        ast_type_or_pred: Union[Type[AdaNode],
                                Callable[[AdaNode], bool]],
        **kwargs: Any
    ) -> Opt[AdaNode]:
        """
        Helper for finditer that will return only the first result. See
        finditer's documentation for more details.
        """
        try:
            return next(self.finditer(ast_type_or_pred, **kwargs))
        except StopIteration:
            return None

    def finditer(
        self,
        ast_type_or_pred: Union[Type[AdaNode],
                                Callable[[AdaNode], bool]],
        **kwargs: Any
    ) -> Iterator[AdaNode]:
        """
        Find every node corresponding to the passed predicates.

        :param ast_type_or_pred: If supplied with a subclass of AdaNode, will
           constrain the resulting collection to only the instances of this
           type or any subclass. If supplied with a predicate, it will apply
           the predicate on every node and keep only the ones for which it
           returns True. If supplied with a list of subclasses of AdaNode, it
           will match all instances of any of them.

        :param kwargs: Allows the user to filter on attributes of the node. For
           every key value association, if the node has an attribute of name
           key that has the specified value, then the child is kept.
        """
        # Create a "pred" function to use as the node filter during the
        # traversal.
        if isinstance(ast_type_or_pred, type):
            sought_type = ast_type_or_pred
            pred = lambda node: isinstance(node, sought_type)
        elif isinstance(ast_type_or_pred, collections.abc.Sequence):
            sought_types = ast_type_or_pred
            pred = lambda node: isinstance(node, tuple(sought_types))
        else:
            pred = ast_type_or_pred

        def match(left, right):
            """
            :param left: Node child to match.
            :param right: Matcher, coming from ``kwargs``.
            """
            if left is None:
                return
            if hasattr(left, "match"):
                return left.match(right)
            else:
                return left == right

        def helper(node):
            for child in node:
                if child is not None:
                    if pred(child):
                        if not kwargs:
                            yield child
                        elif all([match(getattr(child, key, None), val)
                                  for key, val in kwargs.items()]):
                            yield child
                    for c in helper(child):
                        if c is not None:
                            yield c

        return helper(self)

    @property
    def parent_chain(self) -> List[AdaNode]:
        """
        Return the parent chain of self. Self will be the first element,
        followed by the first parent, then this parent's parent, etc.
        """
        def _parent_chain(node):
            yield node
            if node.parent is not None:
                for p in _parent_chain(node.parent):
                    yield p

        return list(_parent_chain(self))

    def __repr__(self) -> str:
        return self.image

    @property
    def tokens(self) -> Iterator[Token]:
        """
        Return an iterator on the range of tokens that self encompasses.
        """
        start = self.token_start
        end = self.token_end

        # All nodes have non-null start/end tokens
        assert start is not None
        assert end is not None

        while not start == end:
            yield start
            next = start.next
            assert next is not None
            start = next
        yield end

    def to_data(self) -> Union[list, dict]:
        """
        Return a nested python data-structure, constituted only of standard
        data types (dicts, lists, strings, ints, etc), and representing the
        portion of the AST corresponding to this node.
        """
        if self.is_list_type:
            return [i.to_data() for i in self if i is not None]
        else:
            return {n: v.to_data()
                    for n, v in self.iter_fields()
                    if v is not None}

    def to_json(self) -> str:
        """
        Return a JSON representation of this node.
        """
        return json.dumps(self.to_data())

    def is_a(self, *types: Type[AdaNode]) -> bool:
        """
        Shortcut for isinstance(self, types). :rtype: bool
        """
        return isinstance(self, tuple(types))

    if TYPE_CHECKING:
        T = TypeVar('T', bound=AdaNode)

    def cast(self, typ: Type[T]) -> T:
        """
        Fluent interface style method. Return ``self``, raise an error if self
        is not of type ``typ``.
        """
        assert isinstance(self, typ)
        return self

    _node_c_type = _hashable_c_pointer()

    @classmethod
    def _wrap(cls, c_value):
        """
        Internal helper to wrap a low-level entity value into an instance of
        the the appropriate high-level Python wrapper subclass.
        """
        node_c_value = c_value.node
        if not node_c_value:
            return None

        rebindings = c_value.info.rebindings
        metadata = c_value.info.md

        # Look for an already existing wrapper for this node
        cache_key = (node_c_value, metadata, rebindings)
        unit = cls._fetch_unit(c_value)
        unit._check_node_cache()
        try:
            return unit._node_cache[cache_key]
        except KeyError:
            pass

        # Pick the right subclass to materialize this node in Python
        kind = _node_kind(ctypes.byref(c_value))
        result = _kind_to_astnode_cls[kind](c_value, node_c_value, rebindings)
        unit._node_cache[cache_key] = result
        return result

    @classmethod
    def _wrap_bare_node(cls, c_value: Any) -> Opt[AdaNode]:
        return cls._wrap(_Entity_c_type.from_bare_node(c_value))

    @classmethod
    def _unwrap(cls, py_value: Opt[AdaNode]) -> Any:
        """
        Internal helper to unwrap a high-level ASTNode instance into a
        low-level value. Raise a TypeError if the input value has unexpected
        type.
        """
        if py_value is None:
            return _Entity_c_type._null_value
        elif not isinstance(py_value, cls):
            _raise_type_error(cls.__name__, py_value)
        else:
            return py_value._c_value

    @property
    def _unsafe_unwrap(self) -> Any:
        """
        Unsafe version of _unwrap, meant for internal uses where we don't want
        to check whether the reference is stale or not.
        """
        return self._unprotected_c_value

    @property
    def _unwrap_einfo(self):
        return self._c_value.info

    @classmethod
    def _fetch_unit(cls, c_value: Any) -> AnalysisUnit:
        return AnalysisUnit._wrap(_node_unit(ctypes.byref(c_value)))

    def _eval_field(self, c_result: Any, c_accessor: Any, *c_args: Any) -> Any:
        """
        Internal helper to evaluate low-level field accessors/properties.

        This calls "c_accessor" on this node with the input arguments and puts
        the result in "c_result". This raises a PropertyError if the evaluation
        failed. Return "c_result" for convenience.
        """
        args = (self._unwrap(self), ) + c_args + (ctypes.byref(c_result), )
        if not c_accessor(*args):
            raise PropertyError()
        return c_result

    def _eval_astnode_field(self, c_accessor: Any) -> Any:
        """
        Internal helper. Wrapper around _eval_field for fields that return an
        AST node and that accept no explicit argument. This is useful as it's
        the most common case of field, so using this wrapper reduces generated
        code length.
        """
        return AdaNode._wrap(
            self._eval_field(_Entity_c_type(), c_accessor)
        )




class AbortNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``abort`` keyword.

    Derived nodes: :py:class:`AbortAbsent`, :py:class:`AbortPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _abort_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class AbortAbsent(AbortNode):
    """
    Subclass of :py:class:`AbortNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AbortNode._field_names + (
    )

    _kind_name = 'AbortAbsent'






class AbortPresent(AbortNode):
    """
    Subclass of :py:class:`AbortNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AbortNode._field_names + (
    )

    _kind_name = 'AbortPresent'






class AbstractNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``abstract`` keyword.

    Derived nodes: :py:class:`AbstractAbsent`, :py:class:`AbstractPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _abstract_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class AbstractAbsent(AbstractNode):
    """
    Subclass of :py:class:`AbstractNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AbstractNode._field_names + (
    )

    _kind_name = 'AbstractAbsent'






class AbstractPresent(AbstractNode):
    """
    Subclass of :py:class:`AbstractNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AbstractNode._field_names + (
    )

    _kind_name = 'AbstractPresent'






class AdaList(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Derived nodes: :py:class:`AdaNodeList`, :py:class:`AspectAssocList`,
    :py:class:`BaseAssocList`, :py:class:`BasicAssocList`,
    :py:class:`CaseExprAlternativeList`, :py:class:`CaseStmtAlternativeList`,
    :py:class:`CompilationUnitList`, :py:class:`ConcatOperandList`,
    :py:class:`ContractCaseAssocList`, :py:class:`DefiningNameList`,
    :py:class:`DiscriminantSpecList`, :py:class:`ElsifExprPartList`,
    :py:class:`ElsifStmtPartList`, :py:class:`EnumLiteralDeclList`,
    :py:class:`ExprList`, :py:class:`FormatStringChunkList`,
    :py:class:`IdentifierList`, :py:class:`NameList`,
    :py:class:`ParamSpecList`, :py:class:`PragmaNodeList`,
    :py:class:`SelectWhenPartList`, :py:class:`UnconstrainedArrayIndexList`,
    :py:class:`VariantList`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class AdaNodeList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of AdaNode.

    This list node can contain one of the following nodes:
    :py:class:`AbstractStateDecl`, :py:class:`AbstractSubpDecl`,
    :py:class:`Allocator`, :py:class:`ArraySubcomponentChoiceName`,
    :py:class:`AspectClause`, :py:class:`AttributeRef`,
    :py:class:`BaseAggregate`, :py:class:`BaseSubpBody`, :py:class:`BinOp`,
    :py:class:`BodyStub`, :py:class:`CallExpr`, :py:class:`CharLiteral`,
    :py:class:`ComponentClause`, :py:class:`ComponentDecl`,
    :py:class:`ConcatOp`, :py:class:`ConcreteTypeDecl`, :py:class:`CondExpr`,
    :py:class:`DeclExpr`, :py:class:`DottedName`, :py:class:`EntryBody`,
    :py:class:`EntryDecl`, :py:class:`ErrorDecl`, :py:class:`ExceptionDecl`,
    :py:class:`ExceptionHandler`, :py:class:`ExplicitDeref`,
    :py:class:`FormatStringLiteral`, :py:class:`GenericDecl`,
    :py:class:`GenericFormal`, :py:class:`GenericInstantiation`,
    :py:class:`GenericRenamingDecl`, :py:class:`Identifier`,
    :py:class:`IncompleteTypeDecl`, :py:class:`MembershipExpr`,
    :py:class:`NullComponentDecl`, :py:class:`NullLiteral`,
    :py:class:`NumLiteral`, :py:class:`NumberDecl`, :py:class:`ObjectDecl`,
    :py:class:`OthersDesignator`, :py:class:`PackageBody`,
    :py:class:`PackageDecl`, :py:class:`PackageRenamingDecl`,
    :py:class:`ParenAbstractStateDecl`, :py:class:`ParenExpr`,
    :py:class:`PragmaNode`, :py:class:`ProtectedBody`,
    :py:class:`ProtectedTypeDecl`, :py:class:`QualExpr`,
    :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
    :py:class:`ReduceAttributeRef`, :py:class:`SingleProtectedDecl`,
    :py:class:`SingleTaskDecl`, :py:class:`Stmt`, :py:class:`StringLiteral`,
    :py:class:`SubpDecl`, :py:class:`SubtypeDecl`,
    :py:class:`SubtypeIndication`, :py:class:`TargetName`,
    :py:class:`TaskBody`, :py:class:`TaskTypeDecl`, :py:class:`UnOp`,
    :py:class:`UpdateAttributeRef`, :py:class:`UseClause`,
    :py:class:`WithClause`

    Derived nodes: :py:class:`AbstractStateDeclList`,
    :py:class:`AlternativesList`, :py:class:`ConstraintList`,
    :py:class:`DeclList`, :py:class:`StmtList`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'AdaNodeList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[AdaNode]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> AdaNode:
        return super().__getitem__(index)  # type: ignore





class AbstractStateDeclList(AdaNodeList):
    """
    Subclass of :py:class:`AdaNodeList`.

    List of AbstractStateDecls.

    This list node can contain one of the following nodes:
    :py:class:`AbstractStateDecl`, :py:class:`ParenAbstractStateDecl`

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNodeList._field_names + (
    )

    _kind_name = 'AbstractStateDeclList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[AdaNode]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> AdaNode:
        return super().__getitem__(index)  # type: ignore





class AlternativesList(AdaNodeList):
    """
    Subclass of :py:class:`AdaNodeList`.

    List of alternatives in a ``when ...`` clause.

    This list node can contain one of the following nodes:
    :py:class:`Allocator`, :py:class:`ArraySubcomponentChoiceName`,
    :py:class:`AttributeRef`, :py:class:`BaseAggregate`, :py:class:`BinOp`,
    :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
    :py:class:`CondExpr`, :py:class:`DeclExpr`,
    :py:class:`DiscreteSubtypeIndication`, :py:class:`DottedName`,
    :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
    :py:class:`Identifier`, :py:class:`MembershipExpr`,
    :py:class:`NullLiteral`, :py:class:`NumLiteral`,
    :py:class:`OthersDesignator`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
    :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
    :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
    :py:class:`TargetName`, :py:class:`UnOp`, :py:class:`UpdateAttributeRef`

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNodeList._field_names + (
    )

    _kind_name = 'AlternativesList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[AdaNode]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> AdaNode:
        return super().__getitem__(index)  # type: ignore





class ConstraintList(AdaNodeList):
    """
    Subclass of :py:class:`AdaNodeList`.

    List of constraints.

    This list node can contain one of the following nodes:
    :py:class:`AttributeRef`, :py:class:`BinOp`, :py:class:`CallExpr`,
    :py:class:`CharLiteral`, :py:class:`DottedName`, :py:class:`ExplicitDeref`,
    :py:class:`Identifier`, :py:class:`QualExpr`,
    :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
    :py:class:`SubtypeIndication`, :py:class:`TargetName`,
    :py:class:`UpdateAttributeRef`

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNodeList._field_names + (
    )

    _kind_name = 'ConstraintList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[AdaNode]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> AdaNode:
        return super().__getitem__(index)  # type: ignore





class DeclList(AdaNodeList):
    """
    Subclass of :py:class:`AdaNodeList`.

    List of declarations.

    This list node can contain one of the following nodes:
    :py:class:`AbstractSubpDecl`, :py:class:`AspectClause`,
    :py:class:`ComponentDecl`, :py:class:`EntryDecl`, :py:class:`ExprFunction`,
    :py:class:`NullSubpDecl`, :py:class:`PragmaNode`, :py:class:`SubpDecl`,
    :py:class:`SubpRenamingDecl`

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNodeList._field_names + (
    )

    _kind_name = 'DeclList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[AdaNode]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> AdaNode:
        return super().__getitem__(index)  # type: ignore





class StmtList(AdaNodeList):
    """
    Subclass of :py:class:`AdaNodeList`.

    List of statements.

    This list node can contain one of the following nodes:
    :py:class:`PragmaNode`, :py:class:`Stmt`

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNodeList._field_names + (
    )

    _kind_name = 'StmtList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[AdaNode]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> AdaNode:
        return super().__getitem__(index)  # type: ignore





class AspectAssocList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of AspectAssoc.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'AspectAssocList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[AspectAssoc]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> AspectAssoc:
        return super().__getitem__(index)  # type: ignore





class BaseAssocList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of BaseAssoc.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'BaseAssocList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[BaseAssoc]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> BaseAssoc:
        return super().__getitem__(index)  # type: ignore





class BasicAssocList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of BasicAssoc.

    Derived nodes: :py:class:`AssocList`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )


    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[BasicAssoc]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> BasicAssoc:
        return super().__getitem__(index)  # type: ignore





class AssocList(BasicAssocList):
    """
    Subclass of :py:class:`BasicAssocList`.

    List of associations.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    def p_zip_with_params(
        self, imprecise_fallback: bool = False
    ) -> List[ParamActual]:
        """
        Returns an array of pairs, associating formal parameters to actual
        expressions. The formals to match are retrieved by resolving the call
        which this AssocList represents the actuals of.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_ParamActualArrayConverter.c_type(), _assoc_list_p_zip_with_params, unwrapped_imprecise_fallback)
        result = _ParamActualArrayConverter.wrap(c_result, False)


        return result

    _field_names = BasicAssocList._field_names + (
    )

    _kind_name = 'AssocList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[BasicAssoc]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> BasicAssoc:
        return super().__getitem__(index)  # type: ignore





class CaseExprAlternativeList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of CaseExprAlternative.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'CaseExprAlternativeList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[CaseExprAlternative]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> CaseExprAlternative:
        return super().__getitem__(index)  # type: ignore





class CaseStmtAlternativeList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of CaseStmtAlternative.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'CaseStmtAlternativeList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[CaseStmtAlternative]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> CaseStmtAlternative:
        return super().__getitem__(index)  # type: ignore





class CompilationUnitList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of CompilationUnit.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'CompilationUnitList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[CompilationUnit]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> CompilationUnit:
        return super().__getitem__(index)  # type: ignore





class ConcatOperandList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of ConcatOperand.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'ConcatOperandList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[ConcatOperand]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> ConcatOperand:
        return super().__getitem__(index)  # type: ignore





class ContractCaseAssocList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of ContractCaseAssoc.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'ContractCaseAssocList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[ContractCaseAssoc]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> ContractCaseAssoc:
        return super().__getitem__(index)  # type: ignore





class DefiningNameList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of DefiningName.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'DefiningNameList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[DefiningName]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> DefiningName:
        return super().__getitem__(index)  # type: ignore





class DiscriminantSpecList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of DiscriminantSpec.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'DiscriminantSpecList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[DiscriminantSpec]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> DiscriminantSpec:
        return super().__getitem__(index)  # type: ignore





class ElsifExprPartList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of ElsifExprPart.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'ElsifExprPartList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[ElsifExprPart]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> ElsifExprPart:
        return super().__getitem__(index)  # type: ignore





class ElsifStmtPartList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of ElsifStmtPart.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'ElsifStmtPartList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[ElsifStmtPart]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> ElsifStmtPart:
        return super().__getitem__(index)  # type: ignore





class EnumLiteralDeclList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of EnumLiteralDecl.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'EnumLiteralDeclList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[EnumLiteralDecl]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> EnumLiteralDecl:
        return super().__getitem__(index)  # type: ignore





class ExprList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of Expr.

    This list node can contain one of the following nodes:
    :py:class:`Allocator`, :py:class:`AttributeRef`, :py:class:`BaseAggregate`,
    :py:class:`BinOp`, :py:class:`CallExpr`, :py:class:`CharLiteral`,
    :py:class:`ConcatOp`, :py:class:`CondExpr`, :py:class:`DeclExpr`,
    :py:class:`DiscreteSubtypeName`, :py:class:`DottedName`,
    :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
    :py:class:`Identifier`, :py:class:`NullLiteral`, :py:class:`NumLiteral`,
    :py:class:`ParenExpr`, :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
    :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
    :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
    :py:class:`UpdateAttributeRef`

    Derived nodes: :py:class:`ExprAlternativesList`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )


    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[Expr]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> Expr:
        return super().__getitem__(index)  # type: ignore





class ExprAlternativesList(ExprList):
    """
    Subclass of :py:class:`ExprList`.

    List of alternatives in a membership test expression.

    This list node can contain one of the following nodes:
    :py:class:`Allocator`, :py:class:`AttributeRef`, :py:class:`BaseAggregate`,
    :py:class:`BinOp`, :py:class:`CallExpr`, :py:class:`CharLiteral`,
    :py:class:`ConcatOp`, :py:class:`CondExpr`, :py:class:`DeclExpr`,
    :py:class:`DiscreteSubtypeName`, :py:class:`DottedName`,
    :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
    :py:class:`Identifier`, :py:class:`NullLiteral`, :py:class:`NumLiteral`,
    :py:class:`ParenExpr`, :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
    :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
    :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
    :py:class:`UpdateAttributeRef`

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ExprList._field_names + (
    )

    _kind_name = 'ExprAlternativesList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[Expr]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> Expr:
        return super().__getitem__(index)  # type: ignore





class FormatStringChunkList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of FormatStringChunk.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'FormatStringChunkList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[FormatStringChunk]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> FormatStringChunk:
        return super().__getitem__(index)  # type: ignore





class IdentifierList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of Identifier.

    Derived nodes: :py:class:`DiscriminantChoiceList`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )


    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[Identifier]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> Identifier:
        return super().__getitem__(index)  # type: ignore





class DiscriminantChoiceList(IdentifierList):
    """
    Subclass of :py:class:`IdentifierList`.

    List of discriminant associations.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = IdentifierList._field_names + (
    )

    _kind_name = 'DiscriminantChoiceList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[Identifier]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> Identifier:
        return super().__getitem__(index)  # type: ignore





class NameList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of Name.

    This list node can contain one of the following nodes:
    :py:class:`AttributeRef`, :py:class:`CallExpr`, :py:class:`CharLiteral`,
    :py:class:`DottedName`, :py:class:`ExplicitDeref`, :py:class:`Identifier`,
    :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
    :py:class:`StringLiteral`, :py:class:`TargetName`,
    :py:class:`UpdateAttributeRef`

    Derived nodes: :py:class:`ParentList`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'NameList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[Name]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> Name:
        return super().__getitem__(index)  # type: ignore





class ParentList(NameList):
    """
    Subclass of :py:class:`NameList`.

    List of parents in a type declaration.

    This list node can contain one of the following nodes:
    :py:class:`CharLiteral`, :py:class:`DottedName`, :py:class:`Identifier`,
    :py:class:`StringLiteral`

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = NameList._field_names + (
    )

    _kind_name = 'ParentList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[Name]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> Name:
        return super().__getitem__(index)  # type: ignore





class ParamSpecList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of ParamSpec.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'ParamSpecList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[ParamSpec]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> ParamSpec:
        return super().__getitem__(index)  # type: ignore





class PragmaNodeList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of Pragma.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'PragmaNodeList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[PragmaNode]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> PragmaNode:
        return super().__getitem__(index)  # type: ignore





class SelectWhenPartList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of SelectWhenPart.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'SelectWhenPartList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[SelectWhenPart]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> SelectWhenPart:
        return super().__getitem__(index)  # type: ignore





class UnconstrainedArrayIndexList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of UnconstrainedArrayIndex.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'UnconstrainedArrayIndexList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[UnconstrainedArrayIndex]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> UnconstrainedArrayIndex:
        return super().__getitem__(index)  # type: ignore





class VariantList(AdaList):
    """
    Subclass of :py:class:`AdaList`.

    List of Variant.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaList._field_names + (
    )

    _kind_name = 'VariantList'

    is_list_type = True

    def __iter__(
        self
    ) -> Iterator[Variant]:
        return super().__iter__()  # type: ignore

    def __getitem__(
        self,
        index: int
    ) -> Variant:
        return super().__getitem__(index)  # type: ignore





class AliasedNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``aliased`` keyword.

    Derived nodes: :py:class:`AliasedAbsent`, :py:class:`AliasedPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _aliased_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class AliasedAbsent(AliasedNode):
    """
    Subclass of :py:class:`AliasedNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AliasedNode._field_names + (
    )

    _kind_name = 'AliasedAbsent'






class AliasedPresent(AliasedNode):
    """
    Subclass of :py:class:`AliasedNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AliasedNode._field_names + (
    )

    _kind_name = 'AliasedPresent'






class AllNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``all`` keyword.

    Derived nodes: :py:class:`AllAbsent`, :py:class:`AllPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _all_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class AllAbsent(AllNode):
    """
    Subclass of :py:class:`AllNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AllNode._field_names + (
    )

    _kind_name = 'AllAbsent'






class AllPresent(AllNode):
    """
    Subclass of :py:class:`AllNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AllNode._field_names + (
    )

    _kind_name = 'AllPresent'






class ArrayIndices(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Specification for array indexes (:rmlink:`3.6`).

    Derived nodes: :py:class:`ConstrainedArrayIndices`,
    :py:class:`UnconstrainedArrayIndices`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class ConstrainedArrayIndices(ArrayIndices):
    """
    Subclass of :py:class:`ArrayIndices`.

    Constrained specification for array indexes (:rmlink:`3.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_list(
        self
    ) -> ConstraintList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`AttributeRef`, :py:class:`BinOp`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`SubtypeIndication`,
        :py:class:`TargetName`, :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_constrained_array_indices_f_list)



        return result

    _field_names = ArrayIndices._field_names + (
        "f_list",
    )

    _kind_name = 'ConstrainedArrayIndices'






class UnconstrainedArrayIndices(ArrayIndices):
    """
    Subclass of :py:class:`ArrayIndices`.

    Unconstrained specification for array indexes (:rmlink:`3.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_types(
        self
    ) -> UnconstrainedArrayIndexList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_unconstrained_array_indices_f_types)



        return result

    _field_names = ArrayIndices._field_names + (
        "f_types",
    )

    _kind_name = 'UnconstrainedArrayIndices'






class AspectAssoc(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Name/expression association in an aspect.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_id(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_aspect_assoc_f_id)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`AbstractStateDeclExpr`, :py:class:`Allocator`,
        :py:class:`AttributeRef`, :py:class:`BaseAggregate`, :py:class:`BinOp`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
        :py:class:`CondExpr`, :py:class:`ContractCases`, :py:class:`DeclExpr`,
        :py:class:`DottedName`, :py:class:`ExplicitDeref`,
        :py:class:`FormatStringLiteral`, :py:class:`Identifier`,
        :py:class:`MembershipExpr`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_aspect_assoc_f_expr)



        return result
    
    @property
    def p_is_ghost_code(
        self
    ) -> bool:
        """
        Return whether this aspect is ghost code or not. See SPARK RM 6.9.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _aspect_assoc_p_is_ghost_code)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
        "f_id",
        "f_expr",
    )

    _kind_name = 'AspectAssoc'






class AspectClause(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Base class for aspect clauses.

    Derived nodes: :py:class:`AtClause`, :py:class:`AttributeDefClause`,
    :py:class:`EnumRepClause`, :py:class:`RecordRepClause`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class AtClause(AspectClause):
    """
    Subclass of :py:class:`AspectClause`.

    Representation clause (``for .. use at ...;``) (:rmlink:`13.5.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> BaseId:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`Identifier`,
        :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_at_clause_f_name)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_at_clause_f_expr)



        return result

    _field_names = AspectClause._field_names + (
        "f_name",
        "f_expr",
    )

    _kind_name = 'AtClause'






class AttributeDefClause(AspectClause):
    """
    Subclass of :py:class:`AspectClause`.

    Clause for an attribute definition (``for ...'Attribute use ...;``)
    (:rmlink:`13.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_attribute_expr(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_attribute_def_clause_f_attribute_expr)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_attribute_def_clause_f_expr)



        return result

    _field_names = AspectClause._field_names + (
        "f_attribute_expr",
        "f_expr",
    )

    _kind_name = 'AttributeDefClause'






class EnumRepClause(AspectClause):
    """
    Subclass of :py:class:`AspectClause`.

    Representation clause for enumeration types (:rmlink:`13.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_type_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_enum_rep_clause_f_type_name)



        return result
    
    @property
    def f_aggregate(
        self
    ) -> BaseAggregate:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_enum_rep_clause_f_aggregate)



        return result
    
    @property
    def p_params(
        self
    ) -> List[ParamActual]:
        """
        Returns an array of pairs, associating enum literals to representation
        clause actuals.
        """
        

        


        
        c_result = self._eval_field(_ParamActualArrayConverter.c_type(), _enum_rep_clause_p_params)
        result = _ParamActualArrayConverter.wrap(c_result, False)


        return result

    _field_names = AspectClause._field_names + (
        "f_type_name",
        "f_aggregate",
    )

    _kind_name = 'EnumRepClause'






class RecordRepClause(AspectClause):
    """
    Subclass of :py:class:`AspectClause`.

    Representation clause for a record type (:rmlink:`13.5.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_record_rep_clause_f_name)



        return result
    
    @property
    def f_at_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_record_rep_clause_f_at_expr)



        return result
    
    @property
    def f_components(
        self
    ) -> AdaNodeList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`ComponentClause`, :py:class:`PragmaNode`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_record_rep_clause_f_components)



        return result

    _field_names = AspectClause._field_names + (
        "f_name",
        "f_at_expr",
        "f_components",
    )

    _kind_name = 'RecordRepClause'






class AspectSpec(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    List of aspects in a declaration (:rmlink:`13.1.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_aspect_assocs(
        self
    ) -> AspectAssocList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_aspect_spec_f_aspect_assocs)



        return result

    _field_names = AdaNode._field_names + (
        "f_aspect_assocs",
    )

    _kind_name = 'AspectSpec'






class BaseAssoc(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Abstract class for a key/value association, where the value is an
    expression.

    Derived nodes: :py:class:`ContractCaseAssoc`,
    :py:class:`PragmaArgumentAssoc`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_assoc_expr(
        self
    ) -> Expr:
        """
        Returns the expression side of this assoc node.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _base_assoc_p_assoc_expr)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = AdaNode._field_names + (
    )







class ContractCaseAssoc(BaseAssoc):
    """
    Subclass of :py:class:`BaseAssoc`.

    Single association for the ``Contract_Case`` aspect.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_guard(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`,
        :py:class:`OthersDesignator`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_contract_case_assoc_f_guard)



        return result
    
    @property
    def f_consequence(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_contract_case_assoc_f_consequence)



        return result

    _field_names = BaseAssoc._field_names + (
        "f_guard",
        "f_consequence",
    )

    _kind_name = 'ContractCaseAssoc'






class PragmaArgumentAssoc(BaseAssoc):
    """
    Subclass of :py:class:`BaseAssoc`.

    Argument association in a pragma.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`Identifier`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_pragma_argument_assoc_f_name)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_pragma_argument_assoc_f_expr)



        return result

    _field_names = BaseAssoc._field_names + (
        "f_name",
        "f_expr",
    )

    _kind_name = 'PragmaArgumentAssoc'






class BaseFormalParamHolder(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Base class for lists of formal parameters. This is used in every case a
    list of "formals" can be called or instantiated, so in all the following
    cases:

    * Subprogram specifications (and subprogram calls).

    * Component lists (and aggregates).

    * Generic formals (and generic instantiations).

    This allows to share the parameter unpacking/matching logic.

    This is a Libadalang abstraction that has no existence in the Ada reference
    manual.

    Derived nodes: :py:class:`BaseSubpSpec`, :py:class:`ComponentList`,
    :py:class:`DiscriminantPart`, :py:class:`EntryCompletionFormalParams`,
    :py:class:`GenericFormalPart`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_abstract_formal_params(
        self
    ) -> List[BaseFormalParamDecl]:
        """
        Return the list of abstract formal parameters for this holder.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _base_formal_param_holder_p_abstract_formal_params)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_formal_params(
        self
    ) -> List[DefiningName]:
        """
        Return all parameters as a ``DefiningName`` array. This property
        doesn't return record discriminants nor variants when called on a
        record component list.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _base_formal_param_holder_p_formal_params)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_nb_min_params(
        self
    ) -> int:
        """
        Return the minimum number of parameters this subprogram can be called
        while still being a legal call.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_int(), _base_formal_param_holder_p_nb_min_params)
        result = c_result.value


        return result
    
    @property
    def p_nb_max_params(
        self
    ) -> int:
        """
        Return the maximum number of parameters this subprogram can be called
        while still being a legal call.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_int(), _base_formal_param_holder_p_nb_max_params)
        result = c_result.value


        return result
    
    def p_param_types(
        self, origin: AdaNode = None
    ) -> List[BaseTypeDecl]:
        """
        Returns the type of each parameter of self.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _base_formal_param_holder_p_param_types, unwrapped_origin)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result

    _field_names = AdaNode._field_names + (
    )







class BaseSubpSpec(BaseFormalParamHolder):
    """
    Subclass of :py:class:`BaseFormalParamHolder`.

    Base class for subprogram specifications (:rmlink:`6.1`).

    Derived nodes: :py:class:`EntrySpec`, :py:class:`EnumSubpSpec`,
    :py:class:`SubpSpec`, :py:class:`SyntheticBinarySpec`,
    :py:class:`SyntheticUnarySpec`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_name(
        self
    ) -> DefiningName:
        """
        Syntax property. Return the name of the subprogram defined by this
        specification.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _base_subp_spec_p_name)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_returns(
        self
    ) -> TypeExpr:
        """
        Syntax property. Return the type expression node corresponding to the
        return of this subprogram spec.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _base_subp_spec_p_returns)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_primitive_subp_types(
        self, imprecise_fallback: bool = False
    ) -> List[BaseTypeDecl]:
        """
        Return the types of which this subprogram is a primitive of.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _base_subp_spec_p_primitive_subp_types, unwrapped_imprecise_fallback)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_primitive_subp_first_type(
        self, imprecise_fallback: bool = False
    ) -> BaseTypeDecl:
        """
        Return the first type of which this subprogram is a primitive of.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _base_subp_spec_p_primitive_subp_first_type, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_primitive_subp_tagged_type(
        self, imprecise_fallback: bool = False
    ) -> BaseTypeDecl:
        """
        If this subprogram is a primitive for a tagged type, then return this
        type.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _base_subp_spec_p_primitive_subp_tagged_type, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_return_type(
        self, origin: AdaNode = None
    ) -> BaseTypeDecl:
        """
        Returns the return type of self, if applicable (e.g. if self is a
        subprogram). Else, returns null.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _base_subp_spec_p_return_type, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_params(
        self
    ) -> List[ParamSpec]:
        """
        Returns the array of parameters specification for this subprogram spec.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _base_subp_spec_p_params)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result

    _field_names = BaseFormalParamHolder._field_names + (
    )







class EntrySpec(BaseSubpSpec):
    """
    Subclass of :py:class:`BaseSubpSpec`.

    Entry specification.

    This node does not have ARM existence, because in the RM subprogram
    specifications don't encompass the ad-hoc specifications that happen in
    entry declarations. Entry declarations are described in :rmlink:`9.5.2`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_entry_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_entry_spec_f_entry_name)



        return result
    
    @property
    def f_family_type(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`SubtypeIndication`,
        :py:class:`TargetName`, :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_entry_spec_f_family_type)



        return result
    
    @property
    def f_entry_params(
        self
    ) -> Params:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_entry_spec_f_entry_params)



        return result

    _field_names = BaseSubpSpec._field_names + (
        "f_entry_name",
        "f_family_type",
        "f_entry_params",
    )

    _kind_name = 'EntrySpec'






class EnumSubpSpec(BaseSubpSpec):
    """
    Subclass of :py:class:`BaseSubpSpec`.

    Synthetic node for the abstract subprogram spec of an enum literal.

    NOTE: This has no existence in the ARM. While enum literals are functions
    semantically, they're not such syntactically.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseSubpSpec._field_names + (
    )

    _kind_name = 'EnumSubpSpec'






class SubpSpec(BaseSubpSpec):
    """
    Subclass of :py:class:`BaseSubpSpec`.

    Subprogram specification (:rmlink:`6.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_subp_kind(
        self
    ) -> SubpKind:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subp_spec_f_subp_kind)



        return result
    
    @property
    def f_subp_name(
        self
    ) -> DefiningName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_subp_spec_f_subp_name)



        return result
    
    @property
    def f_subp_params(
        self
    ) -> Params:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_subp_spec_f_subp_params)



        return result
    
    @property
    def f_subp_returns(
        self
    ) -> TypeExpr:
        """
        This field can contain one of the following nodes:
        :py:class:`AnonymousType`, :py:class:`SubtypeIndication`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_subp_spec_f_subp_returns)



        return result

    _field_names = BaseSubpSpec._field_names + (
        "f_subp_kind",
        "f_subp_name",
        "f_subp_params",
        "f_subp_returns",
    )

    _kind_name = 'SubpSpec'






class SyntheticBinarySpec(BaseSubpSpec):
    """
    Subclass of :py:class:`BaseSubpSpec`.

    Synthetic subprogram specification for binary operators.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_left_param(
        self
    ) -> SyntheticFormalParamDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_synthetic_binary_spec_f_left_param)



        return result
    
    @property
    def f_right_param(
        self
    ) -> SyntheticFormalParamDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_synthetic_binary_spec_f_right_param)



        return result
    
    @property
    def f_return_type_expr(
        self
    ) -> TypeExpr:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_synthetic_binary_spec_f_return_type_expr)



        return result

    _field_names = BaseSubpSpec._field_names + (
        "f_left_param",
        "f_right_param",
        "f_return_type_expr",
    )

    _kind_name = 'SyntheticBinarySpec'






class SyntheticUnarySpec(BaseSubpSpec):
    """
    Subclass of :py:class:`BaseSubpSpec`.

    Synthetic subprogram specification for unary operators.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_right_param(
        self
    ) -> SyntheticFormalParamDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_synthetic_unary_spec_f_right_param)



        return result
    
    @property
    def f_return_type_expr(
        self
    ) -> SyntheticTypeExpr:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_synthetic_unary_spec_f_return_type_expr)



        return result

    _field_names = BaseSubpSpec._field_names + (
        "f_right_param",
        "f_return_type_expr",
    )

    _kind_name = 'SyntheticUnarySpec'






class ComponentList(BaseFormalParamHolder):
    """
    Subclass of :py:class:`BaseFormalParamHolder`.

    List of component declarations (:rmlink:`3.8`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_components(
        self
    ) -> AdaNodeList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`AspectClause`, :py:class:`ComponentDecl`,
        :py:class:`NullComponentDecl`, :py:class:`PragmaNode`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_component_list_f_components)



        return result
    
    @property
    def f_variant_part(
        self
    ) -> VariantPart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_component_list_f_variant_part)



        return result

    _field_names = BaseFormalParamHolder._field_names + (
        "f_components",
        "f_variant_part",
    )

    _kind_name = 'ComponentList'






class DiscriminantPart(BaseFormalParamHolder):
    """
    Subclass of :py:class:`BaseFormalParamHolder`.

    Specification for discriminants in type declarations.

    Derived nodes: :py:class:`KnownDiscriminantPart`,
    :py:class:`UnknownDiscriminantPart`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseFormalParamHolder._field_names + (
    )







class KnownDiscriminantPart(DiscriminantPart):
    """
    Subclass of :py:class:`DiscriminantPart`.

    Known list of discriminants in type declarations (:rmlink:`3.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_discr_specs(
        self
    ) -> DiscriminantSpecList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_known_discriminant_part_f_discr_specs)



        return result

    _field_names = DiscriminantPart._field_names + (
        "f_discr_specs",
    )

    _kind_name = 'KnownDiscriminantPart'






class UnknownDiscriminantPart(DiscriminantPart):
    """
    Subclass of :py:class:`DiscriminantPart`.

    Unknown list of discriminants in type declarations (:rmlink:`3.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = DiscriminantPart._field_names + (
    )

    _kind_name = 'UnknownDiscriminantPart'






class EntryCompletionFormalParams(BaseFormalParamHolder):
    """
    Subclass of :py:class:`BaseFormalParamHolder`.

    Formal parameters for the completion of an ``EntryDecl`` (either an
    ``EntryBody`` or an ``AcceptStmt``).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_params(
        self
    ) -> Params:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_entry_completion_formal_params_f_params)



        return result

    _field_names = BaseFormalParamHolder._field_names + (
        "f_params",
    )

    _kind_name = 'EntryCompletionFormalParams'






class GenericFormalPart(BaseFormalParamHolder):
    """
    Subclass of :py:class:`BaseFormalParamHolder`.

    List of declaration for generic formals (:rmlink:`12.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decls(
        self
    ) -> AdaNodeList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`GenericFormal`, :py:class:`PragmaNode`,
        :py:class:`UseClause`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_formal_part_f_decls)



        return result

    _field_names = BaseFormalParamHolder._field_names + (
        "f_decls",
    )

    _kind_name = 'GenericFormalPart'






class BaseRecordDef(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Base class for record definitions (:rmlink:`3.8`).

    Derived nodes: :py:class:`NullRecordDef`, :py:class:`RecordDef`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_components(
        self
    ) -> ComponentList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_base_record_def_f_components)



        return result

    _field_names = AdaNode._field_names + (
        "f_components",
    )







class NullRecordDef(BaseRecordDef):
    """
    Subclass of :py:class:`BaseRecordDef`.

    Record definition for ``null record``.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseRecordDef._field_names + (
    )

    _kind_name = 'NullRecordDef'






class RecordDef(BaseRecordDef):
    """
    Subclass of :py:class:`BaseRecordDef`.

    Record definition that contains components (``record ... end record``).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseRecordDef._field_names + (
    )

    _kind_name = 'RecordDef'






class BasicAssoc(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Association of one or several names to an expression.

    Derived nodes: :py:class:`AggregateAssoc`,
    :py:class:`CompositeConstraintAssoc`, :py:class:`IteratedAssoc`,
    :py:class:`ParamAssoc`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    def p_get_params(
        self, imprecise_fallback: bool = False
    ) -> List[DefiningName]:
        """
        Return the list of parameters that this association refers to.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _basic_assoc_p_get_params, unwrapped_imprecise_fallback)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result

    _field_names = AdaNode._field_names + (
    )







class AggregateAssoc(BasicAssoc):
    """
    Subclass of :py:class:`BasicAssoc`.

    Association (X => Y) used for aggregates associations (:rmlink:`4.3`).

    Derived nodes: :py:class:`MultiDimArrayAssoc`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_designators(
        self
    ) -> AlternativesList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`Allocator`, :py:class:`ArraySubcomponentChoiceName`,
        :py:class:`AttributeRef`, :py:class:`BaseAggregate`, :py:class:`BinOp`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
        :py:class:`CondExpr`, :py:class:`DeclExpr`,
        :py:class:`DiscreteSubtypeIndication`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`,
        :py:class:`OthersDesignator`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_aggregate_assoc_f_designators)



        return result
    
    @property
    def f_r_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`BoxExpr`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
        :py:class:`CondExpr`, :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_aggregate_assoc_f_r_expr)



        return result

    _field_names = BasicAssoc._field_names + (
        "f_designators",
        "f_r_expr",
    )

    _kind_name = 'AggregateAssoc'






class MultiDimArrayAssoc(AggregateAssoc):
    """
    Subclass of :py:class:`AggregateAssoc`.

    Association used for multi-dimension array aggregates.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AggregateAssoc._field_names + (
    )

    _kind_name = 'MultiDimArrayAssoc'






class CompositeConstraintAssoc(BasicAssoc):
    """
    Subclass of :py:class:`BasicAssoc`.

    Association of discriminant names to an expression (:rmlink:`3.7.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_ids(
        self
    ) -> DiscriminantChoiceList:
        """
        This field can contain one of the following nodes:
        :py:class:`IdentifierList`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_composite_constraint_assoc_f_ids)



        return result
    
    @property
    def f_constraint_expr(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DiscreteSubtypeIndication`,
        :py:class:`DottedName`, :py:class:`ExplicitDeref`,
        :py:class:`FormatStringLiteral`, :py:class:`Identifier`,
        :py:class:`MembershipExpr`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_composite_constraint_assoc_f_constraint_expr)



        return result

    _field_names = BasicAssoc._field_names + (
        "f_ids",
        "f_constraint_expr",
    )

    _kind_name = 'CompositeConstraintAssoc'






class IteratedAssoc(BasicAssoc):
    """
    Subclass of :py:class:`BasicAssoc`.

    Iterated association (Ada 2020, :rmlink:`4.3.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_spec(
        self
    ) -> ForLoopSpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_iterated_assoc_f_spec)



        return result
    
    @property
    def f_key_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_iterated_assoc_f_key_expr)



        return result
    
    @property
    def f_r_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_iterated_assoc_f_r_expr)



        return result

    _field_names = BasicAssoc._field_names + (
        "f_spec",
        "f_key_expr",
        "f_r_expr",
    )

    _kind_name = 'IteratedAssoc'






class ParamAssoc(BasicAssoc):
    """
    Subclass of :py:class:`BasicAssoc`.

    Association (X => Y) used for parameter associations (:rmlink:`6.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_designator(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`Identifier`, :py:class:`OthersDesignator`,
        :py:class:`StringLiteral`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_param_assoc_f_designator)



        return result
    
    @property
    def f_r_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`BoxExpr`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
        :py:class:`CondExpr`, :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_param_assoc_f_r_expr)



        return result

    _field_names = BasicAssoc._field_names + (
        "f_designator",
        "f_r_expr",
    )

    _kind_name = 'ParamAssoc'






class BasicDecl(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Root class for an Ada declaration (:rmlink:`3.1`). A declaration associates
    a name with a language entity, for example a type or a variable.

    Derived nodes: :py:class:`AbstractStateDecl`,
    :py:class:`AnonymousExprDecl`, :py:class:`BaseFormalParamDecl`,
    :py:class:`BasePackageDecl`, :py:class:`BaseTypeDecl`,
    :py:class:`BasicSubpDecl`, :py:class:`BodyNode`,
    :py:class:`EntryIndexSpec`, :py:class:`ErrorDecl`,
    :py:class:`ExceptionDecl`, :py:class:`ExceptionHandler`,
    :py:class:`ForLoopVarDecl`, :py:class:`GenericDecl`,
    :py:class:`GenericInstantiation`, :py:class:`GenericRenamingDecl`,
    :py:class:`LabelDecl`, :py:class:`NamedStmtDecl`, :py:class:`NumberDecl`,
    :py:class:`ObjectDecl`, :py:class:`PackageRenamingDecl`,
    :py:class:`SingleProtectedDecl`, :py:class:`SingleTaskDecl`,
    :py:class:`SyntheticObjectDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_aspects(
        self
    ) -> AspectSpec:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_basic_decl_f_aspects)



        return result
    
    @property
    def p_is_formal(
        self
    ) -> bool:
        """
        Whether this decl is the nested decl of a generic formal declaration.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _basic_decl_p_is_formal)
        result = bool(c_result.value)


        return result
    
    @property
    def p_doc_annotations(
        self
    ) -> List[DocAnnotation]:
        """
        Return the documentation annotations associated with this decl.
        Annotations are any comment line of the form:

        .. code::

           --% [annotation_name]: [annotation]

        Raises a property error if the doc is incorrectly formatted.

        .. attention:: This is an experimental feature, so even if it is
           exposed to allow experiments, it is totally unsupported and the API
           and behavior are very likely to change in the future.
        """
        

        


        
        c_result = self._eval_field(_DocAnnotationArrayConverter.c_type(), _basic_decl_p_doc_annotations)
        result = _DocAnnotationArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_doc(
        self
    ) -> str:
        """
        Return the documentation associated with this decl. Raises a property
        error if the doc is incorrectly formatted.

        .. attention:: This is an experimental feature, so even if it is
           exposed to allow experiments, it is totally unsupported and the API
           and behavior are very likely to change in the future.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _basic_decl_p_doc)
        result = _String.wrap(c_result)


        return result
    
    def p_canonical_part(
        self, imprecise_fallback: bool = False
    ) -> BasicDecl:
        """
        Return the canonical part for this decl. In the case of decls composed
        of several parts, the canonical part will be the first part.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_canonical_part, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_all_parts(
        self, imprecise_fallback: bool = False
    ) -> List[BasicDecl]:
        """
        Return all parts that define this entity, sorted from first part to
        last part.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _basic_decl_p_all_parts, unwrapped_imprecise_fallback)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_get_aspect_assoc(
        self, name: str
    ) -> AspectAssoc:
        """
        Return the aspect with name ``name`` for this entity.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_get_aspect_assoc, unwrapped_name)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_get_aspect_spec_expr(
        self, name: str
    ) -> Expr:
        """
        Return the expression associated to the aspect with name ``name`` for
        this entity.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_get_aspect_spec_expr, unwrapped_name)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_get_aspect(
        self, name: str, previous_parts_only: bool = False, imprecise_fallback: bool = False
    ) -> Aspect:
        """
        Return the aspect with name ``name`` associated to this entity.

        Aspects are properties of entities that can be specified by the Ada
        program, either via aspect specifications, pragmas, or attributes.

        See ``DefiningName.P_Get_Aspect`` for more details.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)
        unwrapped_previous_parts_only = bool(previous_parts_only)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(Aspect._c_type(), _basic_decl_p_get_aspect, unwrapped_name, unwrapped_previous_parts_only, unwrapped_imprecise_fallback)
        result = Aspect._wrap(c_result)


        return result
    
    def p_has_aspect(
        self, name: str, previous_parts_only: bool = False, imprecise_fallback: bool = False
    ) -> bool:
        """
        Returns whether the boolean aspect named ``name`` is set on the entity
        represented by this node.

        Aspects are properties of entities that can be specified by the Ada
        program, either via aspect specifications, pragmas, or attributes.

        "Aspect" is used as in RM terminology (see :rmlink:`13`).
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)
        unwrapped_previous_parts_only = bool(previous_parts_only)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _basic_decl_p_has_aspect, unwrapped_name, unwrapped_previous_parts_only, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    def p_get_pragma(
        self, name: str
    ) -> PragmaNode:
        """
        Return the pragma with name ``name`` associated to this entity.

        Please use the ``p_get_aspect`` property instead if you are interested
        in aspects, i.e. information that can be represented by either aspect
        specification nodes, pragma nodes or attribute definition nodes.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_get_pragma, unwrapped_name)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_get_representation_clause(
        self, name: str, imprecise_fallback: bool = False
    ) -> AttributeDefClause:
        """
        Return the representation clause associated to this type decl that
        defines the given attribute name.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_get_representation_clause, unwrapped_name, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_get_at_clause(
        self, imprecise_fallback: bool = False
    ) -> AtClause:
        """
        Return the at clause associated to this declaration.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_get_at_clause, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_get_annotations(
        self, imprecise_fallback: bool = False
    ) -> List[Aspect]:
        """
        Return all the ``Annotate`` aspects defined on this entity, both
        through pragmas and aspect specifications. For a type declaration, this
        also includes all annotations defined on its from a base type, when
        relevant (the field ``inherited`` will be set for those). See
        ``DefiningName.P_Get_Annotations`` for more details.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AspectArrayConverter.c_type(), _basic_decl_p_get_annotations, unwrapped_imprecise_fallback)
        result = _AspectArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_is_ghost_code(
        self
    ) -> bool:
        """
        Return whether this declaration is ghost code or not. See SPARK RM 6.9.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _basic_decl_p_is_ghost_code)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_compilation_unit_root(
        self
    ) -> bool:
        """
        Whether a BasicDecl is the root decl for its unit.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _basic_decl_p_is_compilation_unit_root)
        result = bool(c_result.value)


        return result
    
    def p_is_visible(
        self, from_node: AdaNode
    ) -> bool:
        """
        Return whether this declaration is visible from the point of view of
        the given ``origin`` node.

        .. attention:: Only package-level (public or private) declarations are
           supported for now.
        """
        

        

        unwrapped_from_node = AdaNode._unwrap(from_node)

        
        c_result = self._eval_field(ctypes.c_uint8(), _basic_decl_p_is_visible, unwrapped_from_node)
        result = bool(c_result.value)


        return result
    
    def p_base_subp_declarations(
        self, imprecise_fallback: bool = False
    ) -> List[BasicDecl]:
        """
        If self declares a primitive subprogram of some tagged type T, return
        the set of all subprogram declarations that it overrides (including
        itself).
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _basic_decl_p_base_subp_declarations, unwrapped_imprecise_fallback)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_root_subp_declarations(
        self, origin: AdaNode = None, imprecise_fallback: bool = False
    ) -> List[BasicDecl]:
        """
        If self declares a primitive subprogram of some type T, return the root
        subprogram declarations that it overrides. There can be several, as in
        the following scenario:

        * package Root defines the root tagged type T and subprogram Foo.

        * package Itf defines interface I and abstract subprogram Foo.

        * package D defines "type U is new Root.T and Itf.I" and an overriding
          subprogram Foo.

        Here, root_subp_declarations of Foo defined in package D will return
        both Foo from package Root and Foo from package Itf.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _basic_decl_p_root_subp_declarations, unwrapped_origin, unwrapped_imprecise_fallback)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_find_all_overrides(
        self, units: List[AnalysisUnit], imprecise_fallback: bool = False
    ) -> List[BasicDecl]:
        """
        If self is the declaration of a primitive of some type T, return the
        list of all subprogram that override this subprogram among the given
        units.
        """
        

        

        unwrapped_units = _AnalysisUnitArrayConverter.unwrap(units)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _basic_decl_p_find_all_overrides, unwrapped_units.c_value, unwrapped_imprecise_fallback)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_defining_names(
        self
    ) -> List[DefiningName]:
        """
        Get all the names of this basic declaration.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _basic_decl_p_defining_names)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_defining_name(
        self
    ) -> DefiningName:
        """
        Get the name of this declaration. If this declaration has several
        names, it will return the first one.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_defining_name)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_subp_spec_or_null(
        self, follow_generic: bool = True
    ) -> BaseSubpSpec:
        """
        If self is a Subp, returns the specification of this subprogram.

        If ``follow_generic`` is True, will also work for instances of
        ``GenericSubpDecl``.
        """
        

        

        unwrapped_follow_generic = bool(follow_generic)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_subp_spec_or_null, unwrapped_follow_generic)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_is_subprogram(
        self
    ) -> bool:
        """
        Return True if self is a subprogram node in the general sense (which
        is, an entity that can be called). This includes separates and entries.

        .. attention: This is a purely syntactic query and will return True for
           everything that is a syntactic entity that can be called like a
           subprogram in some contexts, even generic formal subprograms for
           example.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _basic_decl_p_is_subprogram)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_predefined_operator(
        self
    ) -> bool:
        """
        Return whether this declaration corresponds to a subprogram that
        represents a predefined operator.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _basic_decl_p_is_predefined_operator)
        result = bool(c_result.value)


        return result
    
    @property
    def p_relative_name(
        self
    ) -> Name:
        """
        Return the relative name for self. If self's defining name is
        ``A.B.C``, return ``C`` as a node.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_relative_name)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_relative_name_text(
        self
    ) -> str:
        """
        Return the relative name for self, as text.
        """
        

        


        
        c_result = self._eval_field(_symbol_type(), _basic_decl_p_relative_name_text)
        result = _symbol_type.wrap(c_result)


        return result
    
    def p_body_part_for_decl(
        self, imprecise_fallback: bool = False
    ) -> BodyNode:
        """
        Return the body corresponding to this declaration, if applicable.

        .. note:: It is not named body_part, subclasses have more precise
           versions named body_part and returning a more precise result.
           Probably, we want to rename the specific versions, and have the root
           property be named body_part. (TODO R925-008)
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_body_part_for_decl, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_most_visible_part(
        self, origin: AdaNode, imprecise_fallback: bool = False
    ) -> BasicDecl:
        """
        Given an origin node and the entity represented by self, this property
        returns the most visible completion of self that can be seen by origin,
        according to Ada's visibility rules.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_most_visible_part, unwrapped_origin, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_fully_qualified_name_array(
        self, include_profile: bool = False
    ) -> List[str]:
        """
        Return the fully qualified name corresponding to this declaration, as
        an array of symbols.

        .. note:: See ``BasicDecl.fully_qualified_name`` to know which kind of
           declarations are supported.
        """
        

        

        unwrapped_include_profile = bool(include_profile)

        
        c_result = self._eval_field(_UnboundedTextTypeArrayConverter.c_type(), _basic_decl_p_fully_qualified_name_array, unwrapped_include_profile)
        result = _UnboundedTextTypeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_fully_qualified_name(
        self
    ) -> str:
        """
        Return the fully qualified name corresponding to this declaration.

        .. note:: This property should only be called on declarations that have
           exactly one defining name (excepted for anonymous declarations of
           access types), otherwise a ``PreconditionFailure`` will be raised.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _basic_decl_p_fully_qualified_name)
        result = _String.wrap(c_result)


        return result
    
    @property
    def p_canonical_fully_qualified_name(
        self
    ) -> str:
        """
        Return a canonical representation of the fully qualified name
        corresponding to this declaration.

        .. note:: See ``BasicDecl.fully_qualified_name`` to know which kind of
           declarations are supported.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _basic_decl_p_canonical_fully_qualified_name)
        result = _String.wrap(c_result)


        return result
    
    @property
    def p_unique_identifying_name(
        self
    ) -> str:
        """
        Return a unique identifying name for this declaration, provided this
        declaration is a public declaration. In the case of subprograms, this
        will include the profile.

        .. note:: This property should only be called on declarations that have
           exactly one defining name (excepted for anonymous declarations of
           access types), otherwise a ``PreconditionFailure`` will be raised.

        .. attention:: This will only return a unique name for public
           declarations. Notably, anything nested in an unnamed declare block
           won't be handled correctly.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _basic_decl_p_unique_identifying_name)
        result = _String.wrap(c_result)


        return result
    
    def p_previous_part_for_decl(
        self, imprecise_fallback: bool = False
    ) -> BasicDecl:
        """
        Return the previous part for this decl, if applicable.

        .. note:: It is not named previous_part, because BaseTypeDecl has a
           more precise version of previous_part that returns a BaseTypeDecl.
           Probably, we want to rename the specific versions, and have the root
           property be named previous_part. (TODO R925-008)
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_previous_part_for_decl, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_is_static_decl(
        self, imprecise_fallback: bool = False
    ) -> bool:
        """
        Return whether this declaration is static.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _basic_decl_p_is_static_decl, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_imported(
        self
    ) -> bool:
        """
        Whether this declaration is imported from another language.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _basic_decl_p_is_imported)
        result = bool(c_result.value)


        return result
    
    @property
    def p_type_expression(
        self
    ) -> TypeExpr:
        """
        Return the type expression for this BasicDecl if applicable, a null
        otherwise.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_type_expression)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_next_part_for_decl(
        self, imprecise_fallback: bool = False
    ) -> BasicDecl:
        """
        Return the next part of this declaration, if applicable.

        .. note:: It is not named next_part, because BaseTypeDecl has a more
           precise version of next_part that returns a BaseTypeDecl. Probably,
           we want to rename the specific versions, and have the root property
           be named next_part. (TODO R925-008)
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _basic_decl_p_next_part_for_decl, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_is_constant_object(
        self
    ) -> bool:
        """
        Return whether this object is constant or not.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _basic_decl_p_is_constant_object)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class AbstractStateDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Contained (directly or indirectly) in an AbstractStateDeclExpr, and is used
    to represent the BasicDecl associated with the abstract state introduced by
    the Abstract_State aspect. This node is necessary because all of our name
    resolution routines expect BasicDecls as environments' values.

    The only purpose of this node is to populate the env with the abstract
    state declared through this node, so it can be referred in SPARK aspects
    such as Global, Depends, Refined_State, etc.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_abstract_state_decl_f_name)



        return result

    _field_names = BasicDecl._field_names + (
        "f_name",
        "f_aspects",
    )

    _kind_name = 'AbstractStateDecl'






class AnonymousExprDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Represents a anonymous declaration that holds an expression.

    This is used to store the results of queries such as ``referenced_decl``
    called on references to object formals from inside a instantiated generic
    in order to return the relevant actual.

    Indeed, ``referenced_decl`` must return a ``BasicDecl``, but actuals of
    generic instantiations are ``Expr``. This wrapper node is therefore a way
    to both satisfy the ``BasicDecl`` interface, and provide to the user the
    expression of the actual through the ``expr`` field.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> SyntheticDefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_anonymous_expr_decl_f_name)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        The expression wrapped by this declaration.

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_anonymous_expr_decl_f_expr)



        return result
    
    def p_get_formal(
        self, imprecise_fallback: bool = False
    ) -> DefiningName:
        """
        Return the generic formal object declaration corresponding to this
        actual.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _anonymous_expr_decl_p_get_formal, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BasicDecl._field_names + (
        "f_name",
        "f_expr",
    )

    _kind_name = 'AnonymousExprDecl'






class BaseFormalParamDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Base class for formal parameter declarations. This is used both for records
    components and for subprogram parameters.

    This is a Libadalang abstraction, that has no ARM existence.

    Derived nodes: :py:class:`ComponentDecl`, :py:class:`DiscriminantSpec`,
    :py:class:`GenericFormal`, :py:class:`ParamSpec`,
    :py:class:`SyntheticFormalParamDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    def p_formal_type(
        self, origin: AdaNode = None
    ) -> BaseTypeDecl:
        """
        Return the type for this formal.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _base_formal_param_decl_p_formal_type, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BasicDecl._field_names + (
    )







class ComponentDecl(BaseFormalParamDecl):
    """
    Subclass of :py:class:`BaseFormalParamDecl`.

    Declaration for a component (:rmlink:`3.8`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_ids(
        self
    ) -> DefiningNameList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_component_decl_f_ids)



        return result
    
    @property
    def f_component_def(
        self
    ) -> ComponentDef:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_component_decl_f_component_def)



        return result
    
    @property
    def f_default_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_component_decl_f_default_expr)



        return result

    _field_names = BaseFormalParamDecl._field_names + (
        "f_ids",
        "f_component_def",
        "f_default_expr",
        "f_aspects",
    )

    _kind_name = 'ComponentDecl'






class DiscriminantSpec(BaseFormalParamDecl):
    """
    Subclass of :py:class:`BaseFormalParamDecl`.

    Known list of discriminants in type declarations (:rmlink:`3.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_ids(
        self
    ) -> DefiningNameList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_discriminant_spec_f_ids)



        return result
    
    @property
    def f_type_expr(
        self
    ) -> TypeExpr:
        """
        This field can contain one of the following nodes:
        :py:class:`AnonymousType`, :py:class:`SubtypeIndication`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_discriminant_spec_f_type_expr)



        return result
    
    @property
    def f_default_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_discriminant_spec_f_default_expr)



        return result

    _field_names = BaseFormalParamDecl._field_names + (
        "f_ids",
        "f_type_expr",
        "f_default_expr",
        "f_aspects",
    )

    _kind_name = 'DiscriminantSpec'






class GenericFormal(BaseFormalParamDecl):
    """
    Subclass of :py:class:`BaseFormalParamDecl`.

    Enclosing declaration for a generic formal. The real declaration is
    accessible via the ``decl`` field.

    Derived nodes: :py:class:`GenericFormalObjDecl`,
    :py:class:`GenericFormalPackage`, :py:class:`GenericFormalSubpDecl`,
    :py:class:`GenericFormalTypeDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decl(
        self
    ) -> BasicDecl:
        """
        This field can contain one of the following nodes:
        :py:class:`ExprFunction`, :py:class:`FormalSubpDecl`,
        :py:class:`FormalTypeDecl`, :py:class:`GenericInstantiation`,
        :py:class:`IncompleteFormalTypeDecl`, :py:class:`ObjectDecl`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_formal_f_decl)



        return result

    _field_names = BaseFormalParamDecl._field_names + (
        "f_decl",
    )







class GenericFormalObjDecl(GenericFormal):
    """
    Subclass of :py:class:`GenericFormal`.

    Formal declaration for an object.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = GenericFormal._field_names + (
    )

    _kind_name = 'GenericFormalObjDecl'






class GenericFormalPackage(GenericFormal):
    """
    Subclass of :py:class:`GenericFormal`.

    Formal declaration for a package (:rmlink:`12.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = GenericFormal._field_names + (
    )

    _kind_name = 'GenericFormalPackage'






class GenericFormalSubpDecl(GenericFormal):
    """
    Subclass of :py:class:`GenericFormal`.

    Formal declaration for a subprogram (:rmlink:`12.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = GenericFormal._field_names + (
    )

    _kind_name = 'GenericFormalSubpDecl'






class GenericFormalTypeDecl(GenericFormal):
    """
    Subclass of :py:class:`GenericFormal`.

    Formal declaration for a type (:rmlink:`12.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = GenericFormal._field_names + (
    )

    _kind_name = 'GenericFormalTypeDecl'






class ParamSpec(BaseFormalParamDecl):
    """
    Subclass of :py:class:`BaseFormalParamDecl`.

    Specification for a parameter (:rmlink:`6.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_ids(
        self
    ) -> DefiningNameList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_param_spec_f_ids)



        return result
    
    @property
    def f_has_aliased(
        self
    ) -> AliasedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_param_spec_f_has_aliased)



        return result
    
    @property
    def f_mode(
        self
    ) -> Mode:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_param_spec_f_mode)



        return result
    
    @property
    def f_type_expr(
        self
    ) -> TypeExpr:
        """
        This field can contain one of the following nodes:
        :py:class:`AnonymousType`, :py:class:`SubtypeIndication`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_param_spec_f_type_expr)



        return result
    
    @property
    def f_default_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_param_spec_f_default_expr)



        return result

    _field_names = BaseFormalParamDecl._field_names + (
        "f_ids",
        "f_has_aliased",
        "f_mode",
        "f_type_expr",
        "f_default_expr",
        "f_aspects",
    )

    _kind_name = 'ParamSpec'






class SyntheticFormalParamDecl(BaseFormalParamDecl):
    """
    Subclass of :py:class:`BaseFormalParamDecl`.

    Synthetic parameter declaration.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_param_type(
        self
    ) -> TypeExpr:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_synthetic_formal_param_decl_f_param_type)



        return result

    _field_names = BaseFormalParamDecl._field_names + (
        "f_param_type",
    )

    _kind_name = 'SyntheticFormalParamDecl'






class BasePackageDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Base class for package declarations. This will be used both for non-generic
    package declarations (via :py:class:`PackageDecl`) and for generic ones
    (via :py:class:`GenericPackageInternal`).

    Derived nodes: :py:class:`GenericPackageInternal`, :py:class:`PackageDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_package_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_base_package_decl_f_package_name)



        return result
    
    @property
    def f_public_part(
        self
    ) -> PublicPart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_base_package_decl_f_public_part)



        return result
    
    @property
    def f_private_part(
        self
    ) -> PrivatePart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_base_package_decl_f_private_part)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_base_package_decl_f_end_name)



        return result
    
    @property
    def p_body_part(
        self
    ) -> PackageBody:
        """
        Return the PackageBody corresponding to this node.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _base_package_decl_p_body_part)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BasicDecl._field_names + (
        "f_package_name",
        "f_aspects",
        "f_public_part",
        "f_private_part",
        "f_end_name",
    )







class GenericPackageInternal(BasePackageDecl):
    """
    Subclass of :py:class:`BasePackageDecl`.

    This class denotes the internal package contained by a GenericPackageDecl.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BasePackageDecl._field_names + (
    )

    _kind_name = 'GenericPackageInternal'






class PackageDecl(BasePackageDecl):
    """
    Subclass of :py:class:`BasePackageDecl`.

    Non-generic package declarations (:rmlink:`7.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BasePackageDecl._field_names + (
    )

    _kind_name = 'PackageDecl'






class BaseTypeDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Base class for type declarations. It unifies every kind of type that exists
    in Ada, including types that have no source existence like classwide types.

    Derived nodes: :py:class:`BaseSubtypeDecl`, :py:class:`ClasswideTypeDecl`,
    :py:class:`IncompleteTypeDecl`, :py:class:`ProtectedTypeDecl`,
    :py:class:`TaskTypeDecl`, :py:class:`TypeDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_base_type_decl_f_name)



        return result
    
    def p_base_subtype(
        self, origin: AdaNode = None
    ) -> BaseTypeDecl:
        """
        If this type decl is a subtype decl, return the base subtype. If not,
        return ``self``.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_base_subtype, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_attribute_subprogram(
        self, attr_name: str
    ) -> BasicDecl:
        """
        Return the subprogram declaration denoted by this attribute name and
        defined on this type.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_attr_name = _symbol_type.unwrap(attr_name, _context)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_attribute_subprogram, unwrapped_attr_name)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_private_completion(
        self
    ) -> BaseTypeDecl:
        """
        Return the private completion for this type, if there is one.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_private_completion)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_is_inherited_primitive(
        self, p: BasicDecl
    ) -> bool:
        """
        Assuming that P is a primitive of self, return whether the given
        primitive P is inherited from one of self's parents.
        """
        

        

        unwrapped_p = BasicDecl._unwrap(p)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_inherited_primitive, unwrapped_p)
        result = bool(c_result.value)


        return result
    
    def p_get_record_representation_clause(
        self, imprecise_fallback: bool = False
    ) -> RecordRepClause:
        """
        Return the record representation clause associated to this type decl,
        if applicable (i.e. this type decl defines a record type).
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_get_record_representation_clause, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_get_enum_representation_clause(
        self, imprecise_fallback: bool = False
    ) -> EnumRepClause:
        """
        Return the enum representation clause associated to this type decl, if
        applicable (i.e. this type decl defines an enum type).
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_get_enum_representation_clause, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_get_primitives(
        self, only_inherited: bool = False, include_predefined_operators: bool = False
    ) -> List[BasicDecl]:
        """
        Return the list of all primitive operations that are available on this
        type. If ``only_inherited`` is True, it will only return the primitives
        that are implicitly inherited by this type, discarding those explicitly
        defined on this type. Predefined operators are included in the result
        iff ``include_predefined_operators`` is True. It defaults to False.
        """
        

        

        unwrapped_only_inherited = bool(only_inherited)
        unwrapped_include_predefined_operators = bool(include_predefined_operators)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _base_type_decl_p_get_primitives, unwrapped_only_inherited, unwrapped_include_predefined_operators)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_is_array_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Return whether this type is an array type.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_array_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_find_derived_types(
        self, root: AdaNode, origin: AdaNode, imprecise_fallback: bool = False
    ) -> List[TypeDecl]:
        """
        Find types derived from self in the given ``root`` and its children.
        Self is assumed to be canonicalized.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_root = AdaNode._unwrap(root)
        unwrapped_origin = AdaNode._unwrap(origin)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _base_type_decl_p_find_derived_types, unwrapped_root, unwrapped_origin, unwrapped_imprecise_fallback)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_is_scalar_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Whether type is a scalar type.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_scalar_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    @property
    def p_specific_type(
        self
    ) -> BaseTypeDecl:
        """
        Return the specific type under a class-wide type. Consider for example:

        .. code:: ada

           subtype S1 is T'Class
           subtype S2 is S1'Class

        Calling this property on ``S2`` will return ``T``.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_specific_type)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_find_all_derived_types(
        self, units: List[AnalysisUnit], imprecise_fallback: bool = False
    ) -> List[TypeDecl]:
        """
        Return the list of all types that inherit (directly or indirectly) from
        self among the given units.
        """
        

        

        unwrapped_units = _AnalysisUnitArrayConverter.unwrap(units)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _base_type_decl_p_find_all_derived_types, unwrapped_units.c_value, unwrapped_imprecise_fallback)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_comp_type(
        self, is_subscript: bool = False, origin: AdaNode = None
    ) -> BaseTypeDecl:
        """
        Return the component type of ``self``, if applicable. The component
        type is the type you'll get if you call a value whose type is ``self``.
        So it can either be:

        1. The component type for an array.

        2. The return type for an access to function.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_is_subscript = bool(is_subscript)
        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_comp_type, unwrapped_is_subscript, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_index_type(
        self, dim: int, origin: AdaNode = None
    ) -> BaseTypeDecl:
        """
        Return the index type for dimension ``dim`` for this type, if
        applicable.

        .. warning:: ``dim`` is 0-based, so the first ``index_type`` is at
           index 0.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_dim = int(dim)
        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_index_type, unwrapped_dim, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_is_derived_type(
        self, other_type: BaseTypeDecl, origin: AdaNode = None
    ) -> bool:
        """
        Whether self is derived from other_type.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_other_type = BaseTypeDecl._unwrap(other_type)
        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_derived_type, unwrapped_other_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_limited_type(
        self
    ) -> bool:
        """
        Return True iff this type is limited, either because it is explicitly
        marked as such, or because it inherits from a limited type or has a
        component of a limited type. Also note that protected types and task
        types are limited by definition. Moreover, note that Ada requires all
        parts of a type to agree of its limitedness (e.g. the public view of a
        type must indicate that it is limited if its private completion ends up
        being limited), hence this property does not require looking at any
        other part of the type to determine its limitedness, excepted for
        incomplete type declarations. This implies that for illegal code where
        several parts don't agree, this property will return the result for the
        particular view of the type on which this property is called.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_limited_type)
        result = bool(c_result.value)


        return result
    
    def p_matching_type(
        self, expected_type: BaseTypeDecl, origin: AdaNode = None
    ) -> bool:
        """
        Return whether ``self`` matches ``expected_type``.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_expected_type = BaseTypeDecl._unwrap(expected_type)
        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_matching_type, unwrapped_expected_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_previous_part(
        self, go_to_incomplete: bool = True
    ) -> BaseTypeDecl:
        """
        Returns the previous part for this type decl.
        """
        

        

        unwrapped_go_to_incomplete = bool(go_to_incomplete)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_previous_part, unwrapped_go_to_incomplete)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_next_part(
        self
    ) -> BaseTypeDecl:
        """
        Returns the next part for this type decl.

        .. note:: Since this property returns a ``BaseTypeDecl``, it cannot be
           used to retrieve the next part of ``TaskTypeDecl`` and
           ``ProtectedTypeDecl`` nodes as their next part is actually a
           ``Body``. Use ``BasicDecl.next_part_for_decl`` for those instead.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_next_part)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_full_view(
        self
    ) -> BaseTypeDecl:
        """
        Return the full completion of this type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_full_view)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_is_definite_subtype(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Returns whether this is a definite subtype.

        For convenience, this will return ``False`` for incomplete types, even
        though the correct answer is more akin to "non applicable".

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_definite_subtype, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_statically_constrained(
        self
    ) -> bool:
        """
        Returns whether this is a statically constained subtype (i.e., its size
        is known at compile time).

        All types are statically constrained, expect for records and arrays
        when they are unconstrained.

        For convenience, this will return ``False`` for incomplete types, even
        though the correct answer is more akin to "non applicable".
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_statically_constrained)
        result = bool(c_result.value)


        return result
    
    def p_discriminants_list(
        self, stop_recurse_at: BaseTypeDecl = None, origin: AdaNode = None
    ) -> List[BaseFormalParamDecl]:
        """
        Return the list of all discriminants of this type. If this type has no
        discriminant or only unknown discriminants, an empty list is returned.

        In order to obtain all the discriminants of an extended type, this
        property looks on parents, recursively.

        Extended aggregates can be build from any intermediate parent of an
        extended type. In that case, this property shouldn't recurse to the
        root type, but the one used as the aggregate's ancestor, designated by
        ``stop_recurse_at``.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_stop_recurse_at = BaseTypeDecl._unwrap(stop_recurse_at)
        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _base_type_decl_p_discriminants_list, unwrapped_stop_recurse_at, unwrapped_origin)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_shapes(
        self, include_discriminants: bool = True, origin: AdaNode = None
    ) -> List[Shape]:
        """
        Must be called on a record (sub-)type declaration. Return all the
        possible shapes that a value of this record type can take. For example,
        consider the following record definition:

        .. code::

           type R (A : Integer; B : Integer) is record
               X : Integer;
               case A is
                   when 1 .. 10 =>
                       Y_1 : Integer;
                       case B is
                           when 1 .. 10 =>
                               Z_1 : Integer;
                           when others => null;
                       end case;
                   when 11 .. 20 =>
                       Y_2 : Integer;
                       case B is
                           when 1 .. 10 =>
                               Z_2 : Integer;
                           when others => null;
                       end case;
                   when others => null;
               end case;
           end record;

        For this instance, this property will return the following results:

        .. code::

           [
               [X, Y_1, Z_1],
               [X, Y_1],
               [X, Y_2, Z_2],
               [X, Y_2],
               [X]
           ]

        .. attention:: This property is inaccurate when called on a record
           extension which defines components under a certain condition C, and
           this same condition is used to define some components in the parent
           record: in that case, any feasible shape will in practice contain
           either both the components defined under condition C in the child
           record and the parent record, or none of them.However, due to the
           simplified algorithm we use here to compute the feasible shapes, we
           will also return shapes that include the components of the child
           record but not the parent record, and conversely.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_include_discriminants = bool(include_discriminants)
        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_ShapeArrayConverter.c_type(), _base_type_decl_p_shapes, unwrapped_include_discriminants, unwrapped_origin)
        result = _ShapeArrayConverter.wrap(c_result, False)


        return result
    
    def p_is_record_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Return whether this type is a record type.

        .. attention:: Private tagged types extending public tagged records are
           not considered as record types.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_record_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_is_real_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Whether type is a real type or not.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_real_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_is_float_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Whether type is a float type or not.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_float_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_is_fixed_point(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Whether type is a fixed point type or not.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_fixed_point, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_is_enum_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Whether type is an enum type

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_enum_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_is_access_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Whether self is an access type or not

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_access_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_is_char_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Whether type is a character type or not

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_char_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    @property
    def p_classwide_type(
        self
    ) -> ClasswideTypeDecl:
        """
        Return the classwide type for this type, if applicable
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_classwide_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_discrete_range(
        self
    ) -> DiscreteRange:
        """
        Return the discrete range for this type decl, if applicable.
        """
        

        


        
        c_result = self._eval_field(DiscreteRange._c_type(), _base_type_decl_p_discrete_range)
        result = DiscreteRange._wrap(c_result)


        return result
    
    def p_is_discrete_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Whether type is a discrete type or not.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_discrete_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_is_int_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Whether type is an integer type or not.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_int_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_accessed_type(
        self, origin: AdaNode = None
    ) -> BaseTypeDecl:
        """
        If this type is an access type, or a type with an Implicit_Dereference
        aspect, return the type of a dereference of an instance of this type.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_accessed_type, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_is_tagged_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Whether type is tagged or not

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_tagged_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_abstract_type(
        self
    ) -> bool:
        """
        Whether the type is abstract or not.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_abstract_type)
        result = bool(c_result.value)


        return result
    
    def p_base_type(
        self, origin: AdaNode = None
    ) -> BaseTypeDecl:
        """
        Return the base type entity for this derived type declaration

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_base_type, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_base_types(
        self, origin: AdaNode = None
    ) -> List[BaseTypeDecl]:
        """
        Return the list of base types for self.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _base_type_decl_p_base_types, unwrapped_origin)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_is_interface_type(
        self, origin: AdaNode = None
    ) -> bool:
        """
        Return True iff this type declaration is an interface definition.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_interface_type, unwrapped_origin)
        result = bool(c_result.value)


        return result
    
    def p_canonical_type(
        self, origin: AdaNode = None
    ) -> BaseTypeDecl:
        """
        Return the canonical type declaration for this type declaration. For
        subtypes, it will return the base type declaration.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_canonical_type, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_is_private(
        self
    ) -> bool:
        """
        Whether node is a private view of corresponding type.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _base_type_decl_p_is_private)
        result = bool(c_result.value)


        return result
    
    def p_root_type(
        self, origin: AdaNode = None
    ) -> BaseTypeDecl:
        """
        Return the type that is at the root of the derivation hierarchy
        (ignoring secondary interfaces derivations for tagged types)

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _base_type_decl_p_root_type, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BasicDecl._field_names + (
        "f_name",
    )







class BaseSubtypeDecl(BaseTypeDecl):
    """
    Subclass of :py:class:`BaseTypeDecl`.

    Base class for subtype declarations (:rmlink:`3.2.2`).

    Derived nodes: :py:class:`DiscreteBaseSubtypeDecl`, :py:class:`SubtypeDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    def p_get_type(
        self, origin: AdaNode = None
    ) -> BaseTypeDecl:
        """
        Get the type for this subtype.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _base_subtype_decl_p_get_type, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BaseTypeDecl._field_names + (
    )







class DiscreteBaseSubtypeDecl(BaseSubtypeDecl):
    """
    Subclass of :py:class:`BaseSubtypeDecl`.

    Specific ``BaseSubtypeDecl`` synthetic subclass for the base type of scalar
    types.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseSubtypeDecl._field_names + (
    )

    _kind_name = 'DiscreteBaseSubtypeDecl'






class SubtypeDecl(BaseSubtypeDecl):
    """
    Subclass of :py:class:`BaseSubtypeDecl`.

    Subtype declaration (:rmlink:`3.2.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_subtype(
        self
    ) -> SubtypeIndication:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subtype_decl_f_subtype)



        return result

    _field_names = BaseSubtypeDecl._field_names + (
        "f_subtype",
        "f_aspects",
    )

    _kind_name = 'SubtypeDecl'






class ClasswideTypeDecl(BaseTypeDecl):
    """
    Subclass of :py:class:`BaseTypeDecl`.

    Synthetic node (not parsed, generated from a property call). Refers to the
    classwide type for a given tagged type (:rmlink:`3.4.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseTypeDecl._field_names + (
    )

    _kind_name = 'ClasswideTypeDecl'






class IncompleteTypeDecl(BaseTypeDecl):
    """
    Subclass of :py:class:`BaseTypeDecl`.

    Incomplete declaration for a type (:rmlink:`12.5`).

    Derived nodes: :py:class:`IncompleteFormalTypeDecl`,
    :py:class:`IncompleteTaggedTypeDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_discriminants(
        self
    ) -> DiscriminantPart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_incomplete_type_decl_f_discriminants)



        return result

    _field_names = BaseTypeDecl._field_names + (
        "f_discriminants",
    )

    _kind_name = 'IncompleteTypeDecl'






class IncompleteFormalTypeDecl(IncompleteTypeDecl):
    """
    Subclass of :py:class:`IncompleteTypeDecl`.

    A formal incomplete type declaration.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_is_tagged(
        self
    ) -> TaggedNode:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_incomplete_formal_type_decl_f_is_tagged)



        return result
    
    @property
    def f_default_type(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_incomplete_formal_type_decl_f_default_type)



        return result

    _field_names = IncompleteTypeDecl._field_names + (
        "f_is_tagged",
        "f_default_type",
    )

    _kind_name = 'IncompleteFormalTypeDecl'






class IncompleteTaggedTypeDecl(IncompleteTypeDecl):
    """
    Subclass of :py:class:`IncompleteTypeDecl`.

    Incomplete declaration for a tagged type.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_abstract(
        self
    ) -> AbstractNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_incomplete_tagged_type_decl_f_has_abstract)



        return result

    _field_names = IncompleteTypeDecl._field_names + (
        "f_has_abstract",
    )

    _kind_name = 'IncompleteTaggedTypeDecl'






class ProtectedTypeDecl(BaseTypeDecl):
    """
    Subclass of :py:class:`BaseTypeDecl`.

    Declaration for a protected type (:rmlink:`9.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_discriminants(
        self
    ) -> DiscriminantPart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_protected_type_decl_f_discriminants)



        return result
    
    @property
    def f_interfaces(
        self
    ) -> ParentList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_protected_type_decl_f_interfaces)



        return result
    
    @property
    def f_definition(
        self
    ) -> ProtectedDef:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_protected_type_decl_f_definition)



        return result

    _field_names = BaseTypeDecl._field_names + (
        "f_discriminants",
        "f_aspects",
        "f_interfaces",
        "f_definition",
    )

    _kind_name = 'ProtectedTypeDecl'






class TaskTypeDecl(BaseTypeDecl):
    """
    Subclass of :py:class:`BaseTypeDecl`.

    Declaration for a task type (:rmlink:`9.1`).

    Derived nodes: :py:class:`SingleTaskTypeDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_discriminants(
        self
    ) -> DiscriminantPart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_task_type_decl_f_discriminants)



        return result
    
    @property
    def f_definition(
        self
    ) -> TaskDef:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_task_type_decl_f_definition)



        return result

    _field_names = BaseTypeDecl._field_names + (
        "f_discriminants",
        "f_aspects",
        "f_definition",
    )

    _kind_name = 'TaskTypeDecl'






class SingleTaskTypeDecl(TaskTypeDecl):
    """
    Subclass of :py:class:`TaskTypeDecl`.

    Type declaration for a single task (:rmlink:`9.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = TaskTypeDecl._field_names + (
    )

    _kind_name = 'SingleTaskTypeDecl'






class TypeDecl(BaseTypeDecl):
    """
    Subclass of :py:class:`BaseTypeDecl`.

    Type declarations that embed a type definition node. Corresponds to the
    ARM's full type declarations (:rmlink:`3.2.1`).

    Derived nodes: :py:class:`AnonymousTypeDecl`, :py:class:`ConcreteTypeDecl`,
    :py:class:`FormalTypeDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_discriminants(
        self
    ) -> DiscriminantPart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_type_decl_f_discriminants)



        return result
    
    @property
    def f_type_def(
        self
    ) -> TypeDef:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_type_decl_f_type_def)



        return result

    _field_names = BaseTypeDecl._field_names + (
        "f_discriminants",
        "f_type_def",
    )







class AnonymousTypeDecl(TypeDecl):
    """
    Subclass of :py:class:`TypeDecl`.

    Anonymous type declaration (for anonymous array or access types). This
    class has no RM existence, and anonymous (sub)types are referred to
    implicitly in the RM.

    Derived nodes: :py:class:`SynthAnonymousTypeDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = TypeDecl._field_names + (
    )

    _kind_name = 'AnonymousTypeDecl'






class SynthAnonymousTypeDecl(AnonymousTypeDecl):
    """
    Subclass of :py:class:`AnonymousTypeDecl`.

    Synthetic anonymous type decl. Used to generate anonymous access types.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AnonymousTypeDecl._field_names + (
    )

    _kind_name = 'SynthAnonymousTypeDecl'






class ConcreteTypeDecl(TypeDecl):
    """
    Subclass of :py:class:`TypeDecl`.

    A concrete type declaration.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = TypeDecl._field_names + (
        "f_aspects",
    )

    _kind_name = 'ConcreteTypeDecl'






class FormalTypeDecl(TypeDecl):
    """
    Subclass of :py:class:`TypeDecl`.

    A formal type declaration.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_default_type(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_formal_type_decl_f_default_type)



        return result

    _field_names = TypeDecl._field_names + (
        "f_default_type",
        "f_aspects",
    )

    _kind_name = 'FormalTypeDecl'






class BasicSubpDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Base class for subprogram declarations.

    Derived nodes: :py:class:`ClassicSubpDecl`, :py:class:`EntryDecl`,
    :py:class:`EnumLiteralDecl`, :py:class:`GenericSubpInternal`,
    :py:class:`SyntheticSubpDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_subp_decl_spec(
        self
    ) -> BaseSubpSpec:
        """
        Return the specification for this subprogram
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _basic_subp_decl_p_subp_decl_spec)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BasicDecl._field_names + (
    )







class ClassicSubpDecl(BasicSubpDecl):
    """
    Subclass of :py:class:`BasicSubpDecl`.

    This is an intermediate abstract class for subprogram declarations with a
    common structure: overriding indicator, ``SubpSpec``, aspects, <other
    fields>.

    Derived nodes: :py:class:`AbstractSubpDecl`, :py:class:`FormalSubpDecl`,
    :py:class:`SubpDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_overriding(
        self
    ) -> OverridingNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_classic_subp_decl_f_overriding)



        return result
    
    @property
    def f_subp_spec(
        self
    ) -> SubpSpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_classic_subp_decl_f_subp_spec)



        return result
    
    def p_body_part(
        self, imprecise_fallback: bool = False
    ) -> BaseSubpBody:
        """
        Return the BaseSubpBody corresponding to this node.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _classic_subp_decl_p_body_part, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BasicSubpDecl._field_names + (
        "f_overriding",
        "f_subp_spec",
    )







class AbstractSubpDecl(ClassicSubpDecl):
    """
    Subclass of :py:class:`ClassicSubpDecl`.

    Declaration for an abstract subprogram (:rmlink:`3.9.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ClassicSubpDecl._field_names + (
        "f_aspects",
    )

    _kind_name = 'AbstractSubpDecl'






class FormalSubpDecl(ClassicSubpDecl):
    """
    Subclass of :py:class:`ClassicSubpDecl`.

    Formal subprogram declarations, in generic declarations formal parts
    (:rmlink:`12.6`).

    Derived nodes: :py:class:`AbstractFormalSubpDecl`,
    :py:class:`ConcreteFormalSubpDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_default_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`BoxExpr`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`NullLiteral`, :py:class:`QualExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_formal_subp_decl_f_default_expr)



        return result

    _field_names = ClassicSubpDecl._field_names + (
        "f_default_expr",
        "f_aspects",
    )







class AbstractFormalSubpDecl(FormalSubpDecl):
    """
    Subclass of :py:class:`FormalSubpDecl`.

    Formal declaration for an abstract subprogram (:rmlink:`12.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = FormalSubpDecl._field_names + (
    )

    _kind_name = 'AbstractFormalSubpDecl'






class ConcreteFormalSubpDecl(FormalSubpDecl):
    """
    Subclass of :py:class:`FormalSubpDecl`.

    Formal declaration for a concrete subprogram (:rmlink:`12.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = FormalSubpDecl._field_names + (
    )

    _kind_name = 'ConcreteFormalSubpDecl'






class SubpDecl(ClassicSubpDecl):
    """
    Subclass of :py:class:`ClassicSubpDecl`.

    Regular subprogram declaration (:rmlink:`6.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ClassicSubpDecl._field_names + (
        "f_aspects",
    )

    _kind_name = 'SubpDecl'






class EntryDecl(BasicSubpDecl):
    """
    Subclass of :py:class:`BasicSubpDecl`.

    Entry declaration (:rmlink:`9.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_overriding(
        self
    ) -> OverridingNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_entry_decl_f_overriding)



        return result
    
    @property
    def f_spec(
        self
    ) -> EntrySpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_entry_decl_f_spec)



        return result
    
    def p_body_part(
        self, imprecise_fallback: bool = False
    ) -> BodyNode:
        """
        Return the entry body associated to this entry declaration.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _entry_decl_p_body_part, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_accept_stmts(
        self
    ) -> List[AcceptStmt]:
        """
        Return an array of accept statements corresponding to this entry.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _entry_decl_p_accept_stmts)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result

    _field_names = BasicSubpDecl._field_names + (
        "f_overriding",
        "f_spec",
        "f_aspects",
    )

    _kind_name = 'EntryDecl'






class EnumLiteralDecl(BasicSubpDecl):
    """
    Subclass of :py:class:`BasicSubpDecl`.

    Declaration for an enumeration literal (:rmlink:`3.5.1`).

    Derived nodes: :py:class:`SyntheticCharEnumLit`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_enum_literal_decl_f_name)



        return result
    
    @property
    def p_enum_rep(
        self
    ) -> int:
        """
        Return the integer used to encode this enum literal.

        .. note:: This property is equivalent to GNAT's ``Enum_Rep`` attribute.
        """
        

        


        
        c_result = self._eval_field(_big_integer.c_type(), _enum_literal_decl_p_enum_rep)
        result = _big_integer.wrap(c_result)


        return result
    
    @property
    def p_enum_type(
        self
    ) -> TypeDecl:
        """
        Return the enum type corresponding to this enum literal.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _enum_literal_decl_p_enum_type)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BasicSubpDecl._field_names + (
        "f_name",
    )

    _kind_name = 'EnumLiteralDecl'






class SyntheticCharEnumLit(EnumLiteralDecl):
    """
    Subclass of :py:class:`EnumLiteralDecl`.

    Synthetic character enum literal declaration.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_expr(
        self
    ) -> DefiningName:
        """
        Return the CharLiteral expression corresponding to this enum literal.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _synthetic_char_enum_lit_p_expr)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = EnumLiteralDecl._field_names + (
    )

    _kind_name = 'SyntheticCharEnumLit'






class GenericSubpInternal(BasicSubpDecl):
    """
    Subclass of :py:class:`BasicSubpDecl`.

    Internal node for generic subprograms.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_subp_spec(
        self
    ) -> SubpSpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_subp_internal_f_subp_spec)



        return result

    _field_names = BasicSubpDecl._field_names + (
        "f_subp_spec",
        "f_aspects",
    )

    _kind_name = 'GenericSubpInternal'






class SyntheticSubpDecl(BasicSubpDecl):
    """
    Subclass of :py:class:`BasicSubpDecl`.

    Synthetic subprogram declaration.

    Is used to represent predefined operators. This should also be usable for
    synthesizing function attributes.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_spec(
        self
    ) -> BaseSubpSpec:
        """
        This field can contain one of the following nodes:
        :py:class:`SyntheticBinarySpec`, :py:class:`SyntheticUnarySpec`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_synthetic_subp_decl_f_spec)



        return result

    _field_names = BasicSubpDecl._field_names + (
        "f_spec",
    )

    _kind_name = 'SyntheticSubpDecl'






class BodyNode(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Base class for an Ada body (:rmlink:`3.11`). A body is the completion of a
    declaration.

    Derived nodes: :py:class:`AcceptStmtBody`, :py:class:`BaseSubpBody`,
    :py:class:`BodyStub`, :py:class:`EntryBody`, :py:class:`PackageBody`,
    :py:class:`ProtectedBody`, :py:class:`TaskBody`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    def p_previous_part(
        self, imprecise_fallback: bool = False
    ) -> BasicDecl:
        """
        Return the previous part for this body. Might be a declaration or a
        body stub.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _body_node_p_previous_part, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_decl_part(
        self, imprecise_fallback: bool = False
    ) -> BasicDecl:
        """
        Return the decl corresponding to this node if applicable.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _body_node_p_decl_part, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_subunit_root(
        self
    ) -> BasicDecl:
        """
        If self is a subunit, return the body in which it is rooted.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _body_node_p_subunit_root)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BasicDecl._field_names + (
    )







class AcceptStmtBody(BodyNode):
    """
    Subclass of :py:class:`BodyNode`.

    BasicDecl that is always the declaration of an AcceptStmt. This is nested
    *inside* of the accept statement.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_accept_stmt_body_f_name)



        return result

    _field_names = BodyNode._field_names + (
        "f_name",
    )

    _kind_name = 'AcceptStmtBody'






class BaseSubpBody(BodyNode):
    """
    Subclass of :py:class:`BodyNode`.

    Base class for subprogram bodies (:rmlink:`6.3`).

    Derived nodes: :py:class:`ExprFunction`, :py:class:`NullSubpDecl`,
    :py:class:`SubpBody`, :py:class:`SubpRenamingDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_overriding(
        self
    ) -> OverridingNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_base_subp_body_f_overriding)



        return result
    
    @property
    def f_subp_spec(
        self
    ) -> SubpSpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_base_subp_body_f_subp_spec)



        return result

    _field_names = BodyNode._field_names + (
        "f_overriding",
        "f_subp_spec",
    )







class ExprFunction(BaseSubpBody):
    """
    Subclass of :py:class:`BaseSubpBody`.

    Expression function (:rmlink:`6.8`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`BaseAggregate`, :py:class:`ParenExpr`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_expr_function_f_expr)



        return result

    _field_names = BaseSubpBody._field_names + (
        "f_expr",
        "f_aspects",
    )

    _kind_name = 'ExprFunction'






class NullSubpDecl(BaseSubpBody):
    """
    Subclass of :py:class:`BaseSubpBody`.

    Declaration for a null subprogram (:rmlink:`6.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseSubpBody._field_names + (
        "f_aspects",
    )

    _kind_name = 'NullSubpDecl'






class SubpBody(BaseSubpBody):
    """
    Subclass of :py:class:`BaseSubpBody`.

    Subprogram body(:rmlink:`6.3`) .

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decls(
        self
    ) -> DeclarativePart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subp_body_f_decls)



        return result
    
    @property
    def f_stmts(
        self
    ) -> HandledStmts:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subp_body_f_stmts)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_subp_body_f_end_name)



        return result

    _field_names = BaseSubpBody._field_names + (
        "f_aspects",
        "f_decls",
        "f_stmts",
        "f_end_name",
    )

    _kind_name = 'SubpBody'






class SubpRenamingDecl(BaseSubpBody):
    """
    Subclass of :py:class:`BaseSubpBody`.

    Declaration for a subprogram renaming (:rmlink:`8.5.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_renames(
        self
    ) -> RenamingClause:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subp_renaming_decl_f_renames)



        return result

    _field_names = BaseSubpBody._field_names + (
        "f_renames",
        "f_aspects",
    )

    _kind_name = 'SubpRenamingDecl'






class BodyStub(BodyNode):
    """
    Subclass of :py:class:`BodyNode`.

    Base class for a body stub (:rmlink:`10.1.3`). A body stub is meant to be
    completed by .

    Derived nodes: :py:class:`PackageBodyStub`, :py:class:`ProtectedBodyStub`,
    :py:class:`SubpBodyStub`, :py:class:`TaskBodyStub`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_syntactic_fully_qualified_name(
        self
    ) -> List[str]:
        """
        Return the syntactic fully qualified name to refer to this body.

        Note that this can raise a Property_Error when the stub is in an
        illegal place (too nested, in a declare block, etc.).
        """
        

        


        
        c_result = self._eval_field(_UnboundedTextTypeArrayConverter.c_type(), _body_stub_p_syntactic_fully_qualified_name)
        result = _UnboundedTextTypeArrayConverter.wrap(c_result, False)


        return result

    _field_names = BodyNode._field_names + (
    )







class PackageBodyStub(BodyStub):
    """
    Subclass of :py:class:`BodyStub`.

    Stub for a package body (``is separate``) (:rmlink:`10.1.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_package_body_stub_f_name)



        return result

    _field_names = BodyStub._field_names + (
        "f_name",
        "f_aspects",
    )

    _kind_name = 'PackageBodyStub'






class ProtectedBodyStub(BodyStub):
    """
    Subclass of :py:class:`BodyStub`.

    Stub for a protected object body (``is separate``) (:rmlink:`10.1.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_protected_body_stub_f_name)



        return result

    _field_names = BodyStub._field_names + (
        "f_name",
        "f_aspects",
    )

    _kind_name = 'ProtectedBodyStub'






class SubpBodyStub(BodyStub):
    """
    Subclass of :py:class:`BodyStub`.

    Stub for a subprogram body (``is separate``) (:rmlink:`10.1.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_overriding(
        self
    ) -> OverridingNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subp_body_stub_f_overriding)



        return result
    
    @property
    def f_subp_spec(
        self
    ) -> SubpSpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subp_body_stub_f_subp_spec)



        return result

    _field_names = BodyStub._field_names + (
        "f_overriding",
        "f_subp_spec",
        "f_aspects",
    )

    _kind_name = 'SubpBodyStub'






class TaskBodyStub(BodyStub):
    """
    Subclass of :py:class:`BodyStub`.

    Stub for a task body (``is separate``) (:rmlink:`10.1.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_task_body_stub_f_name)



        return result

    _field_names = BodyStub._field_names + (
        "f_name",
        "f_aspects",
    )

    _kind_name = 'TaskBodyStub'






class EntryBody(BodyNode):
    """
    Subclass of :py:class:`BodyNode`.

    Entry body (:rmlink:`9.5.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_entry_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_entry_body_f_entry_name)



        return result
    
    @property
    def f_index_spec(
        self
    ) -> EntryIndexSpec:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_entry_body_f_index_spec)



        return result
    
    @property
    def f_params(
        self
    ) -> EntryCompletionFormalParams:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_entry_body_f_params)



        return result
    
    @property
    def f_barrier(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_entry_body_f_barrier)



        return result
    
    @property
    def f_decls(
        self
    ) -> DeclarativePart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_entry_body_f_decls)



        return result
    
    @property
    def f_stmts(
        self
    ) -> HandledStmts:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_entry_body_f_stmts)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_entry_body_f_end_name)



        return result

    _field_names = BodyNode._field_names + (
        "f_entry_name",
        "f_index_spec",
        "f_params",
        "f_aspects",
        "f_barrier",
        "f_decls",
        "f_stmts",
        "f_end_name",
    )

    _kind_name = 'EntryBody'






class PackageBody(BodyNode):
    """
    Subclass of :py:class:`BodyNode`.

    Package body (:rmlink:`7.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_package_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_package_body_f_package_name)



        return result
    
    @property
    def f_decls(
        self
    ) -> DeclarativePart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_package_body_f_decls)



        return result
    
    @property
    def f_stmts(
        self
    ) -> HandledStmts:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_package_body_f_stmts)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_package_body_f_end_name)



        return result

    _field_names = BodyNode._field_names + (
        "f_package_name",
        "f_aspects",
        "f_decls",
        "f_stmts",
        "f_end_name",
    )

    _kind_name = 'PackageBody'






class ProtectedBody(BodyNode):
    """
    Subclass of :py:class:`BodyNode`.

    Protected object body (:rmlink:`9.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_protected_body_f_name)



        return result
    
    @property
    def f_decls(
        self
    ) -> DeclarativePart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_protected_body_f_decls)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_protected_body_f_end_name)



        return result

    _field_names = BodyNode._field_names + (
        "f_name",
        "f_aspects",
        "f_decls",
        "f_end_name",
    )

    _kind_name = 'ProtectedBody'






class TaskBody(BodyNode):
    """
    Subclass of :py:class:`BodyNode`.

    Task body (:rmlink:`9.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_task_body_f_name)



        return result
    
    @property
    def f_decls(
        self
    ) -> DeclarativePart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_task_body_f_decls)



        return result
    
    @property
    def f_stmts(
        self
    ) -> HandledStmts:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_task_body_f_stmts)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_task_body_f_end_name)



        return result

    _field_names = BodyNode._field_names + (
        "f_name",
        "f_aspects",
        "f_decls",
        "f_stmts",
        "f_end_name",
    )

    _kind_name = 'TaskBody'






class EntryIndexSpec(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Index specification for an entry body (:rmlink:`9.5.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_id(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_entry_index_spec_f_id)



        return result
    
    @property
    def f_subtype(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`SubtypeIndication`,
        :py:class:`TargetName`, :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_entry_index_spec_f_subtype)



        return result

    _field_names = BasicDecl._field_names + (
        "f_id",
        "f_subtype",
        "f_aspects",
    )

    _kind_name = 'EntryIndexSpec'






class ErrorDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Placeholder node for syntax errors in lists of declarations.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BasicDecl._field_names + (
    )

    _kind_name = 'ErrorDecl'






class ExceptionDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Exception declarations (:rmlink:`11.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_ids(
        self
    ) -> DefiningNameList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_exception_decl_f_ids)



        return result
    
    @property
    def f_renames(
        self
    ) -> RenamingClause:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_exception_decl_f_renames)



        return result

    _field_names = BasicDecl._field_names + (
        "f_ids",
        "f_renames",
        "f_aspects",
    )

    _kind_name = 'ExceptionDecl'






class ExceptionHandler(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Exception handler (:rmlink:`11.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_exception_name(
        self
    ) -> DefiningName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_exception_handler_f_exception_name)



        return result
    
    @property
    def f_handled_exceptions(
        self
    ) -> AlternativesList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`OthersDesignator`, :py:class:`QualExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_exception_handler_f_handled_exceptions)



        return result
    
    @property
    def f_stmts(
        self
    ) -> StmtList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`PragmaNode`, :py:class:`Stmt`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_exception_handler_f_stmts)



        return result

    _field_names = BasicDecl._field_names + (
        "f_exception_name",
        "f_handled_exceptions",
        "f_stmts",
    )

    _kind_name = 'ExceptionHandler'






class ForLoopVarDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Declaration for the controlling variable in a ``for`` loop (:rmlink:`5.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_id(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_for_loop_var_decl_f_id)



        return result
    
    @property
    def f_id_type(
        self
    ) -> TypeExpr:
        """
        This field can contain one of the following nodes:
        :py:class:`AnonymousType`, :py:class:`SubtypeIndication`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_for_loop_var_decl_f_id_type)



        return result

    _field_names = BasicDecl._field_names + (
        "f_id",
        "f_id_type",
    )

    _kind_name = 'ForLoopVarDecl'






class GenericDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Base class for generic declarations (:rmlink:`12.1`).

    Derived nodes: :py:class:`GenericPackageDecl`, :py:class:`GenericSubpDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_formal_part(
        self
    ) -> GenericFormalPart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_decl_f_formal_part)



        return result

    _field_names = BasicDecl._field_names + (
        "f_formal_part",
    )







class GenericPackageDecl(GenericDecl):
    """
    Subclass of :py:class:`GenericDecl`.

    Generic package declaration (:rmlink:`12.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_package_decl(
        self
    ) -> GenericPackageInternal:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_package_decl_f_package_decl)



        return result
    
    @property
    def p_body_part(
        self
    ) -> PackageBody:
        """
        Return the PackageBody corresponding to this node, or null if there is
        none.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _generic_package_decl_p_body_part)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = GenericDecl._field_names + (
        "f_package_decl",
    )

    _kind_name = 'GenericPackageDecl'






class GenericSubpDecl(GenericDecl):
    """
    Subclass of :py:class:`GenericDecl`.

    Generic subprogram declaration (:rmlink:`12.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_subp_decl(
        self
    ) -> GenericSubpInternal:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_subp_decl_f_subp_decl)



        return result
    
    def p_body_part(
        self, imprecise_fallback: bool = False
    ) -> BaseSubpBody:
        """
        Return the BaseSubpBody corresponding to this node.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _generic_subp_decl_p_body_part, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = GenericDecl._field_names + (
        "f_subp_decl",
    )

    _kind_name = 'GenericSubpDecl'






class GenericInstantiation(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Instantiations of generics (:rmlink:`12.3`).

    Derived nodes: :py:class:`GenericPackageInstantiation`,
    :py:class:`GenericSubpInstantiation`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_designated_generic_decl(
        self
    ) -> GenericDecl:
        """
        Return the generic decl entity designated by this instantiation,
        including instantiation information. This is equivalent to the expanded
        generic unit in GNAT.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _generic_instantiation_p_designated_generic_decl)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_inst_params(
        self
    ) -> List[ParamActual]:
        """
        Returns an array of pairs, associating formal parameters to actual or
        default expressions.
        """
        

        


        
        c_result = self._eval_field(_ParamActualArrayConverter.c_type(), _generic_instantiation_p_inst_params)
        result = _ParamActualArrayConverter.wrap(c_result, False)


        return result

    _field_names = BasicDecl._field_names + (
    )







class GenericPackageInstantiation(GenericInstantiation):
    """
    Subclass of :py:class:`GenericInstantiation`.

    Instantiations of a generic package.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_package_instantiation_f_name)



        return result
    
    @property
    def f_generic_pkg_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_package_instantiation_f_generic_pkg_name)



        return result
    
    @property
    def f_params(
        self
    ) -> AssocList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`ParamAssoc`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_package_instantiation_f_params)



        return result

    _field_names = GenericInstantiation._field_names + (
        "f_name",
        "f_generic_pkg_name",
        "f_params",
        "f_aspects",
    )

    _kind_name = 'GenericPackageInstantiation'






class GenericSubpInstantiation(GenericInstantiation):
    """
    Subclass of :py:class:`GenericInstantiation`.

    Instantiations of a generic subprogram .

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_overriding(
        self
    ) -> OverridingNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_subp_instantiation_f_overriding)



        return result
    
    @property
    def f_kind(
        self
    ) -> SubpKind:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_subp_instantiation_f_kind)



        return result
    
    @property
    def f_subp_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_subp_instantiation_f_subp_name)



        return result
    
    @property
    def f_generic_subp_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_subp_instantiation_f_generic_subp_name)



        return result
    
    @property
    def f_params(
        self
    ) -> AssocList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`ParamAssoc`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_subp_instantiation_f_params)



        return result

    _field_names = GenericInstantiation._field_names + (
        "f_overriding",
        "f_kind",
        "f_subp_name",
        "f_generic_subp_name",
        "f_params",
        "f_aspects",
    )

    _kind_name = 'GenericSubpInstantiation'






class GenericRenamingDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Base node for all generic renaming declarations (:rmlink:`8.5.5`).

    Derived nodes: :py:class:`GenericPackageRenamingDecl`,
    :py:class:`GenericSubpRenamingDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BasicDecl._field_names + (
    )







class GenericPackageRenamingDecl(GenericRenamingDecl):
    """
    Subclass of :py:class:`GenericRenamingDecl`.

    Declaration for a generic package renaming (:rmlink:`8.5.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_package_renaming_decl_f_name)



        return result
    
    @property
    def f_renames(
        self
    ) -> RenamingClause:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_package_renaming_decl_f_renames)



        return result

    _field_names = GenericRenamingDecl._field_names + (
        "f_name",
        "f_renames",
        "f_aspects",
    )

    _kind_name = 'GenericPackageRenamingDecl'






class GenericSubpRenamingDecl(GenericRenamingDecl):
    """
    Subclass of :py:class:`GenericRenamingDecl`.

    Declaration for a generic subprogram renaming.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_kind(
        self
    ) -> SubpKind:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_subp_renaming_decl_f_kind)



        return result
    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_subp_renaming_decl_f_name)



        return result
    
    @property
    def f_renames(
        self
    ) -> RenamingClause:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_generic_subp_renaming_decl_f_renames)



        return result

    _field_names = GenericRenamingDecl._field_names + (
        "f_kind",
        "f_name",
        "f_renames",
        "f_aspects",
    )

    _kind_name = 'GenericSubpRenamingDecl'






class LabelDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Declaration for a code label (:rmlink:`5.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_label_decl_f_name)



        return result

    _field_names = BasicDecl._field_names + (
        "f_name",
    )

    _kind_name = 'LabelDecl'






class NamedStmtDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    BasicDecl that is always the declaration inside a named statement.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_named_stmt_decl_f_name)



        return result

    _field_names = BasicDecl._field_names + (
        "f_name",
    )

    _kind_name = 'NamedStmtDecl'






class NumberDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Declaration for a static constant number (:rmlink:`3.3.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_ids(
        self
    ) -> DefiningNameList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_number_decl_f_ids)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_number_decl_f_expr)



        return result

    _field_names = BasicDecl._field_names + (
        "f_ids",
        "f_expr",
    )

    _kind_name = 'NumberDecl'






class ObjectDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Base class for Ada object declarations (:rmlink:`3.3.1`). Ada object
    declarations are variables/constants declarations that can be declared in
    any declarative scope.

    Derived nodes: :py:class:`ExtendedReturnStmtObjectDecl`,
    :py:class:`NoTypeObjectRenamingDecl`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_ids(
        self
    ) -> DefiningNameList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_object_decl_f_ids)



        return result
    
    @property
    def f_has_aliased(
        self
    ) -> AliasedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_object_decl_f_has_aliased)



        return result
    
    @property
    def f_has_constant(
        self
    ) -> ConstantNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_object_decl_f_has_constant)



        return result
    
    @property
    def f_mode(
        self
    ) -> Mode:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_object_decl_f_mode)



        return result
    
    @property
    def f_type_expr(
        self
    ) -> TypeExpr:
        """
        This field can contain one of the following nodes:
        :py:class:`AnonymousType`, :py:class:`SubtypeIndication`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_object_decl_f_type_expr)



        return result
    
    @property
    def f_default_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_object_decl_f_default_expr)



        return result
    
    @property
    def f_renaming_clause(
        self
    ) -> RenamingClause:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_object_decl_f_renaming_clause)



        return result
    
    @property
    def p_is_statically_constrained(
        self
    ) -> bool:
        """
        Returns whether this designates a statically constrained object. An
        object is statically constrained if its nominal subtype is statically
        constrained, or if it is a static string constant.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _object_decl_p_is_statically_constrained)
        result = bool(c_result.value)


        return result
    
    @property
    def p_private_part_decl(
        self
    ) -> BasicDecl:
        """
        If this object decl is the constant completion of an object decl in the
        public part, return the object decl from the public part.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _object_decl_p_private_part_decl)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_public_part_decl(
        self
    ) -> BasicDecl:
        """
        If this object decl is the incomplete declaration of a constant in a
        public part, return its completion in the private part.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _object_decl_p_public_part_decl)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BasicDecl._field_names + (
        "f_ids",
        "f_has_aliased",
        "f_has_constant",
        "f_mode",
        "f_type_expr",
        "f_default_expr",
        "f_renaming_clause",
        "f_aspects",
    )

    _kind_name = 'ObjectDecl'






class ExtendedReturnStmtObjectDecl(ObjectDecl):
    """
    Subclass of :py:class:`ObjectDecl`.

    Object declaration that is part of an extended return statement
    (:rmlink:`6.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ObjectDecl._field_names + (
    )

    _kind_name = 'ExtendedReturnStmtObjectDecl'






class NoTypeObjectRenamingDecl(ObjectDecl):
    """
    Subclass of :py:class:`ObjectDecl`.

    Object declaration without subtype indication. This node has been
    introduced to cover a special case for ``ObjectDecl``, where ``type_expr``
    is made optional (AI12-0275), and therefore cannot fit in an
    ``ObjectDecl``.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ObjectDecl._field_names + (
    )

    _kind_name = 'NoTypeObjectRenamingDecl'






class PackageRenamingDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Declaration for a package renaming (:rmlink:`8.5.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_package_renaming_decl_f_name)



        return result
    
    @property
    def f_renames(
        self
    ) -> RenamingClause:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_package_renaming_decl_f_renames)



        return result
    
    @property
    def p_renamed_package(
        self
    ) -> BasicDecl:
        """
        Return the declaration of the package that is renamed by self.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _package_renaming_decl_p_renamed_package)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_final_renamed_package(
        self
    ) -> BasicDecl:
        """
        Return the declaration of the package that is ultimately renamed by
        self, skipping through all intermediate package renamings.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _package_renaming_decl_p_final_renamed_package)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = BasicDecl._field_names + (
        "f_name",
        "f_renames",
        "f_aspects",
    )

    _kind_name = 'PackageRenamingDecl'






class SingleProtectedDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Declaration for a single protected object (:rmlink:`9.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> DefiningName:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_single_protected_decl_f_name)



        return result
    
    @property
    def f_interfaces(
        self
    ) -> ParentList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_single_protected_decl_f_interfaces)



        return result
    
    @property
    def f_definition(
        self
    ) -> ProtectedDef:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_single_protected_decl_f_definition)



        return result

    _field_names = BasicDecl._field_names + (
        "f_name",
        "f_aspects",
        "f_interfaces",
        "f_definition",
    )

    _kind_name = 'SingleProtectedDecl'






class SingleTaskDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    Declaration for a single task (:rmlink:`9.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_task_type(
        self
    ) -> SingleTaskTypeDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_single_task_decl_f_task_type)



        return result

    _field_names = BasicDecl._field_names + (
        "f_task_type",
    )

    _kind_name = 'SingleTaskDecl'






class SyntheticObjectDecl(BasicDecl):
    """
    Subclass of :py:class:`BasicDecl`.

    SyntheticObjectDecl is a declaration that holds a virtual object. This is
    for example used in type predicates to refer to an object of the enclosing
    type, as in:

    .. code::

       subtype Odd is Natural with
          Dynamic_Predicate => Odd mod 2 = 1;

    where we have to create an object named ``Odd``, and of type ``Odd`` so
    that the name in the aspect expression refers to it and can be properly
    resolved to the type identifier.

    This node has no existance in the Ada RM, it's only used for internal name
    resolution purposes.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BasicDecl._field_names + (
    )

    _kind_name = 'SyntheticObjectDecl'






class CaseStmtAlternative(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Alternative in a ``case`` statement (``when ... => ...``).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_choices(
        self
    ) -> AlternativesList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DiscreteSubtypeIndication`,
        :py:class:`DottedName`, :py:class:`ExplicitDeref`,
        :py:class:`FormatStringLiteral`, :py:class:`Identifier`,
        :py:class:`MembershipExpr`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`OthersDesignator`,
        :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_case_stmt_alternative_f_choices)



        return result
    
    @property
    def f_stmts(
        self
    ) -> StmtList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`PragmaNode`, :py:class:`Stmt`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_case_stmt_alternative_f_stmts)



        return result

    _field_names = AdaNode._field_names + (
        "f_choices",
        "f_stmts",
    )

    _kind_name = 'CaseStmtAlternative'






class CompilationUnit(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Root node for all Ada analysis units (:rmlink:`10.1.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_prelude(
        self
    ) -> AdaNodeList:
        """
        ``with``, ``use`` or ``pragma`` statements.

        This field contains a list that itself contains one of the following
        nodes: :py:class:`PragmaNode`, :py:class:`UseClause`,
        :py:class:`WithClause`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_compilation_unit_f_prelude)



        return result
    
    @property
    def f_body(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`LibraryItem`, :py:class:`Subunit`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_compilation_unit_f_body)



        return result
    
    @property
    def f_pragmas(
        self
    ) -> PragmaNodeList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_compilation_unit_f_pragmas)



        return result
    
    @property
    def p_syntactic_fully_qualified_name(
        self
    ) -> List[str]:
        """
        Return the syntactic fully qualified name of this compilation unit.
        """
        

        


        
        c_result = self._eval_field(_UnboundedTextTypeArrayConverter.c_type(), _compilation_unit_p_syntactic_fully_qualified_name)
        result = _UnboundedTextTypeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_unit_kind(
        self
    ) -> str:
        """
        Return the kind corresponding to this analysis unit.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_int(), _compilation_unit_p_unit_kind)
        result = AnalysisUnitKind._wrap(c_result)


        return result
    
    def p_withed_units(
        self, include_privates: bool = True
    ) -> List[CompilationUnit]:
        """
        Look for all "with" clauses at the top of this compilation unit and
        return all the compilation units designated by them. For the complete
        dependencies list of compilation units, see the ``unit_dependencies``
        property. Units imported with a "private with" are included in this
        list only if ``include_privates`` is True.
        """
        

        

        unwrapped_include_privates = bool(include_privates)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _compilation_unit_p_withed_units, unwrapped_include_privates)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_imported_units(
        self, include_privates: bool = True
    ) -> List[CompilationUnit]:
        """
        Return all the compilation units that are directly imported by this
        one. This includes "with"ed units as well as the direct parent unit.
        Units imported with a "private with" are included in this list only if
        ``include_privates`` is True.
        """
        

        

        unwrapped_include_privates = bool(include_privates)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _compilation_unit_p_imported_units, unwrapped_include_privates)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_unit_dependencies(
        self
    ) -> List[CompilationUnit]:
        """
        Return the list of all the compilation units that are (direct and
        indirect) dependencies of this one. See the
        ``withed_units``/``imported_units`` properties to only get the direct
        dependencies of this unit.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _compilation_unit_p_unit_dependencies)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_decl(
        self
    ) -> BasicDecl:
        """
        Get the root basic decl defined in this compilation unit.
        """
        

        

        result = self._eval_astnode_field(_compilation_unit_p_decl)



        return result
    
    def p_is_preelaborable(
        self, imprecise_fallback: bool = False
    ) -> bool:
        """
        Whether this compilation unit is preelaborable or not.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _compilation_unit_p_is_preelaborable, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    @property
    def p_other_part(
        self
    ) -> CompilationUnit:
        """
        If this compilation unit is of kind UnitSpecification, return its
        corresponding body unit, and conversely.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _compilation_unit_p_other_part)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_has_restriction(
        self, name: str
    ) -> bool:
        """
        Whether this compilation unit is affected by the restriction with the
        given name.

        .. warning:: This property only supports the ``No_Elaboration_Code``
           restriction for now.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)

        
        c_result = self._eval_field(ctypes.c_uint8(), _compilation_unit_p_has_restriction, unwrapped_name)
        result = bool(c_result.value)


        return result
    
    @property
    def p_all_config_pragmas(
        self
    ) -> List[PragmaNode]:
        """
        Return the list of configuration pragmas that apply to the current
        unit.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _compilation_unit_p_all_config_pragmas)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_config_pragmas(
        self, name: str
    ) -> List[PragmaNode]:
        """
        Return the list of configuration pragmas with the given name that apply
        to the current unit.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _compilation_unit_p_config_pragmas, unwrapped_name)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result

    _field_names = AdaNode._field_names + (
        "f_prelude",
        "f_body",
        "f_pragmas",
    )

    _kind_name = 'CompilationUnit'






class ComponentClause(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Representation clause for a single component (:rmlink:`13.5.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_id(
        self
    ) -> Identifier:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_component_clause_f_id)



        return result
    
    @property
    def f_position(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_component_clause_f_position)



        return result
    
    @property
    def f_range(
        self
    ) -> RangeSpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_component_clause_f_range)



        return result

    _field_names = AdaNode._field_names + (
        "f_id",
        "f_position",
        "f_range",
    )

    _kind_name = 'ComponentClause'






class ComponentDef(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Definition for a component (:rmlink:`3.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_aliased(
        self
    ) -> AliasedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_component_def_f_has_aliased)



        return result
    
    @property
    def f_has_constant(
        self
    ) -> ConstantNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_component_def_f_has_constant)



        return result
    
    @property
    def f_type_expr(
        self
    ) -> TypeExpr:
        """
        This field can contain one of the following nodes:
        :py:class:`AnonymousType`, :py:class:`SubtypeIndication`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_component_def_f_type_expr)



        return result

    _field_names = AdaNode._field_names + (
        "f_has_aliased",
        "f_has_constant",
        "f_type_expr",
    )

    _kind_name = 'ComponentDef'






class ConstantNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``constant`` keyword.

    Derived nodes: :py:class:`ConstantAbsent`, :py:class:`ConstantPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _constant_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class ConstantAbsent(ConstantNode):
    """
    Subclass of :py:class:`ConstantNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ConstantNode._field_names + (
    )

    _kind_name = 'ConstantAbsent'






class ConstantPresent(ConstantNode):
    """
    Subclass of :py:class:`ConstantNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ConstantNode._field_names + (
    )

    _kind_name = 'ConstantPresent'






class Constraint(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Base class for type constraints (:rmlink:`3.2.2`).

    Derived nodes: :py:class:`CompositeConstraint`,
    :py:class:`DeltaConstraint`, :py:class:`DigitsConstraint`,
    :py:class:`RangeConstraint`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class CompositeConstraint(Constraint):
    """
    Subclass of :py:class:`Constraint`.

    Constraint for a composite type (:rmlink:`3.6.1`). Due to ambiguities in
    the Ada grammar, this could be either a list of index constraints, if the
    owning type is an array type, or a list of discriminant constraints, if the
    owning type is a discriminated record type.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_constraints(
        self
    ) -> AssocList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`CompositeConstraintAssoc`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_composite_constraint_f_constraints)



        return result
    
    @property
    def p_is_index_constraint(
        self
    ) -> bool:
        """
        Whether this composite constraint is an index constraint.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _composite_constraint_p_is_index_constraint)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_discriminant_constraint(
        self
    ) -> bool:
        """
        Whether this composite constraint is a discriminant constraint.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _composite_constraint_p_is_discriminant_constraint)
        result = bool(c_result.value)


        return result
    
    @property
    def p_discriminant_params(
        self
    ) -> List[ParamActual]:
        """
        Returns an array of pairs, associating each discriminant to its actual
        or default expression.
        """
        

        


        
        c_result = self._eval_field(_ParamActualArrayConverter.c_type(), _composite_constraint_p_discriminant_params)
        result = _ParamActualArrayConverter.wrap(c_result, False)


        return result

    _field_names = Constraint._field_names + (
        "f_constraints",
    )

    _kind_name = 'CompositeConstraint'






class DeltaConstraint(Constraint):
    """
    Subclass of :py:class:`Constraint`.

    Delta and range type constraint (:rmlink:`J.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_delta(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_delta_constraint_f_delta)



        return result
    
    @property
    def f_range(
        self
    ) -> RangeSpec:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_delta_constraint_f_range)



        return result

    _field_names = Constraint._field_names + (
        "f_delta",
        "f_range",
    )

    _kind_name = 'DeltaConstraint'






class DigitsConstraint(Constraint):
    """
    Subclass of :py:class:`Constraint`.

    Digits and range type constraint (:rmlink:`3.5.9`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_digits(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_digits_constraint_f_digits)



        return result
    
    @property
    def f_range(
        self
    ) -> RangeSpec:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_digits_constraint_f_range)



        return result

    _field_names = Constraint._field_names + (
        "f_digits",
        "f_range",
    )

    _kind_name = 'DigitsConstraint'






class RangeConstraint(Constraint):
    """
    Subclass of :py:class:`Constraint`.

    Range-based type constraint (:rmlink:`3.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_range(
        self
    ) -> RangeSpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_range_constraint_f_range)



        return result

    _field_names = Constraint._field_names + (
        "f_range",
    )

    _kind_name = 'RangeConstraint'






class DeclarativePart(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    List of declarations (:rmlink:`3.11`).

    Derived nodes: :py:class:`PrivatePart`, :py:class:`PublicPart`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decls(
        self
    ) -> AdaNodeList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`AbstractSubpDecl`, :py:class:`AspectClause`,
        :py:class:`BaseSubpBody`, :py:class:`BodyStub`,
        :py:class:`ComponentDecl`, :py:class:`ConcreteTypeDecl`,
        :py:class:`EntryBody`, :py:class:`EntryDecl`, :py:class:`ErrorDecl`,
        :py:class:`ExceptionDecl`, :py:class:`GenericDecl`,
        :py:class:`GenericInstantiation`, :py:class:`GenericRenamingDecl`,
        :py:class:`IncompleteTypeDecl`, :py:class:`NumberDecl`,
        :py:class:`ObjectDecl`, :py:class:`PackageBody`,
        :py:class:`PackageDecl`, :py:class:`PackageRenamingDecl`,
        :py:class:`PragmaNode`, :py:class:`ProtectedBody`,
        :py:class:`ProtectedTypeDecl`, :py:class:`SingleProtectedDecl`,
        :py:class:`SingleTaskDecl`, :py:class:`SubpDecl`,
        :py:class:`SubtypeDecl`, :py:class:`TaskBody`,
        :py:class:`TaskTypeDecl`, :py:class:`UseClause`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_declarative_part_f_decls)



        return result

    _field_names = AdaNode._field_names + (
        "f_decls",
    )

    _kind_name = 'DeclarativePart'






class PrivatePart(DeclarativePart):
    """
    Subclass of :py:class:`DeclarativePart`.

    List of declarations in a private part.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = DeclarativePart._field_names + (
    )

    _kind_name = 'PrivatePart'






class PublicPart(DeclarativePart):
    """
    Subclass of :py:class:`DeclarativePart`.

    List of declarations in a public part.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = DeclarativePart._field_names + (
    )

    _kind_name = 'PublicPart'






class ElsePart(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    ``else`` part in an ``if`` or ``select`` statement block.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_stmts(
        self
    ) -> StmtList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`PragmaNode`, :py:class:`Stmt`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_else_part_f_stmts)



        return result

    _field_names = AdaNode._field_names + (
        "f_stmts",
    )

    _kind_name = 'ElsePart'






class ElsifExprPart(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    ``elsif`` block, part of an ``if`` expression.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_cond_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_elsif_expr_part_f_cond_expr)



        return result
    
    @property
    def f_then_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_elsif_expr_part_f_then_expr)



        return result

    _field_names = AdaNode._field_names + (
        "f_cond_expr",
        "f_then_expr",
    )

    _kind_name = 'ElsifExprPart'






class ElsifStmtPart(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    ``elsif`` part in an ``if`` statement block.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_cond_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_elsif_stmt_part_f_cond_expr)



        return result
    
    @property
    def f_stmts(
        self
    ) -> StmtList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`PragmaNode`, :py:class:`Stmt`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_elsif_stmt_part_f_stmts)



        return result

    _field_names = AdaNode._field_names + (
        "f_cond_expr",
        "f_stmts",
    )

    _kind_name = 'ElsifStmtPart'






class Expr(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Base class for expressions (:rmlink:`4.4`).

    Derived nodes: :py:class:`AbstractStateDeclExpr`, :py:class:`Allocator`,
    :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`BoxExpr`,
    :py:class:`CaseExprAlternative`, :py:class:`ConcatOp`,
    :py:class:`ConcatOperand`, :py:class:`CondExpr`, :py:class:`ContractCases`,
    :py:class:`DeclExpr`, :py:class:`FormatStringLiteral`,
    :py:class:`MembershipExpr`, :py:class:`Name`, :py:class:`ParenExpr`,
    :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`, :py:class:`UnOp`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_expression_type(
        self
    ) -> BaseTypeDecl:
        """
        Return the declaration corresponding to the type of this expression
        after name resolution.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _expr_p_expression_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_expected_expression_type(
        self
    ) -> BaseTypeDecl:
        """
        Return the declaration corresponding to the expected type of this
        expression after name resolution.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _expr_p_expected_expression_type)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_is_dynamically_tagged(
        self, imprecise_fallback: bool = False
    ) -> bool:
        """
        Returns whether this expression is dynamically tagged (See
        :rmlink:`3.9.2`).
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _expr_p_is_dynamically_tagged, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    def p_is_static_expr(
        self, imprecise_fallback: bool = False
    ) -> bool:
        """
        Return whether this expression is static according to the ARM
        definition of static. See :rmlink:`4.9`.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _expr_p_is_static_expr, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    @property
    def p_eval_as_int(
        self
    ) -> int:
        """
        Statically evaluates self, and returns the value of the evaluation as
        an integer.

        .. note:: In order for a call to this not to raise, the expression
           needs to be a static expression, as specified in :rmlink:`4.9`. You
           can verify whether an expression is static with the
           ``is_static_expr`` property.

        .. attention:: This is an experimental feature, so even if it is
           exposed to allow experiments, it is totally unsupported and the API
           and behavior are very likely to change in the future.
        """
        

        


        
        c_result = self._eval_field(_big_integer.c_type(), _expr_p_eval_as_int)
        result = _big_integer.wrap(c_result)


        return result
    
    def p_eval_as_int_in_env(
        self, env: List[Substitution]
    ) -> int:
        """
        Statically evaluates self, and returns the value of the evaluation as
        an integer. The given environment is used to substitute references to
        declarations by actual values.

        .. note:: In order for a call to this not to raise, the expression
           needs to be a static expression, as specified in :rmlink:`4.9`. You
           can verify whether an expression is static with the
           ``is_static_expr`` property.

        .. attention:: This is an experimental feature, so even if it is
           exposed to allow experiments, it is totally unsupported and the API
           and behavior are very likely to change in the future.
        """
        

        

        unwrapped_env = _SubstitutionArrayConverter.unwrap(env)

        
        c_result = self._eval_field(_big_integer.c_type(), _expr_p_eval_as_int_in_env, unwrapped_env.c_value)
        result = _big_integer.wrap(c_result)


        return result
    
    @property
    def p_eval_as_string(
        self
    ) -> str:
        """
        Statically evaluates self, and returns the value of the evaluation as a
        string.

        .. note:: In order for a call to this not to raise, the expression
           needs to be a static expression, as specified in :rmlink:`4.9`. You
           can verify whether an expression is static with the
           ``is_static_expr`` property.

        .. attention:: This is an experimental feature, so even if it is
           exposed to allow experiments, it is totally unsupported and the API
           and behavior are very likely to change in the future.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _expr_p_eval_as_string)
        result = _String.wrap(c_result)


        return result
    
    def p_eval_as_string_in_env(
        self, env: List[Substitution]
    ) -> str:
        """
        Statically evaluates self, and returns the value of the evaluation as a
        string. The given environment is used to substitute references to
        declarations by actual values.

        .. note:: In order for a call to this not to raise, the expression
           needs to be a static expression, as specified in :rmlink:`4.9`. You
           can verify whether an expression is static with the
           ``is_static_expr`` property.

        .. attention:: This is an experimental feature, so even if it is
           exposed to allow experiments, it is totally unsupported and the API
           and behavior are very likely to change in the future.
        """
        

        

        unwrapped_env = _SubstitutionArrayConverter.unwrap(env)

        
        c_result = self._eval_field(_String.c_type(), _expr_p_eval_as_string_in_env, unwrapped_env.c_value)
        result = _String.wrap(c_result)


        return result
    
    @property
    def p_matching_nodes(
        self
    ) -> List[AdaNode]:
        """
        Return the list of AST nodes that can be a match for this expression
        before overloading analysis.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _expr_p_matching_nodes)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_is_dispatching_call(
        self, imprecise_fallback: bool = False
    ) -> bool:
        """
        Returns True if this ``Name`` corresponds to a dispatching call,
        including:

        * Calls done through subprogram access types.

        * Calls to dispatching subprograms, in the object-oriented sense.

        .. note:: This is an experimental feature. There might be some
           discrepancy with the GNAT concept of "dispatching call".

        .. note:: This should only be called on a ``Name`` and ``UnOp`` or a
           ``BinOp``.

        .. attention:: There is a known bug, where the ConcatOp node is not
           supported, so calling is_dispatching_call on operators nested inside
           of a concat operator will always return false. (Internal TN:
           VC08-029)
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _expr_p_is_dispatching_call, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    @property
    def p_first_corresponding_decl(
        self
    ) -> BasicDecl:
        """
        Return the first decl that is lexically named like self in self's
        scope.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _expr_p_first_corresponding_decl)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = AdaNode._field_names + (
    )







class AbstractStateDeclExpr(Expr):
    """
    Subclass of :py:class:`Expr`.

    Directly corresponds to the right-hand side of the Abstract_State aspect.
    Only exists because the RHS of an AspectAssoc must be an expression: the
    actual logic is in AbstractStateDecl.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_state_decl(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`AbstractStateDecl`, :py:class:`MultiAbstractStateDecl`,
        :py:class:`ParenAbstractStateDecl`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_abstract_state_decl_expr_f_state_decl)



        return result

    _field_names = Expr._field_names + (
        "f_state_decl",
    )

    _kind_name = 'AbstractStateDeclExpr'






class Allocator(Expr):
    """
    Subclass of :py:class:`Expr`.

    Allocator expression (``new ...``) (:rmlink:`4.8`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_subpool(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_allocator_f_subpool)



        return result
    
    @property
    def f_type_or_expr(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`QualExpr`, :py:class:`SubtypeIndication`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_allocator_f_type_or_expr)



        return result
    
    @property
    def p_get_allocated_type(
        self
    ) -> BaseTypeDecl:
        """
        Return the allocated type for this allocator.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _allocator_p_get_allocated_type)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = Expr._field_names + (
        "f_subpool",
        "f_type_or_expr",
    )

    _kind_name = 'Allocator'






class BaseAggregate(Expr):
    """
    Subclass of :py:class:`Expr`.

    Base class for aggregates (:rmlink:`4.3`).

    Derived nodes: :py:class:`Aggregate`, :py:class:`DeltaAggregate`,
    :py:class:`NullRecordAggregate`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_ancestor_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_base_aggregate_f_ancestor_expr)



        return result
    
    @property
    def f_assocs(
        self
    ) -> AssocList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`AggregateAssoc`, :py:class:`IteratedAssoc`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_base_aggregate_f_assocs)



        return result
    
    @property
    def p_aggregate_params(
        self
    ) -> List[ParamActual]:
        """
        Returns an array of pairs, associating formal parameters to actual
        expressions. See ``zip_with_params``.
        """
        

        


        
        c_result = self._eval_field(_ParamActualArrayConverter.c_type(), _base_aggregate_p_aggregate_params)
        result = _ParamActualArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_is_subaggregate(
        self
    ) -> bool:
        """
        Return whether this aggregate is actually a subaggregate of a
        multidimensional array aggregate, as described in :rmlink:`4.3.3`.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _base_aggregate_p_is_subaggregate)
        result = bool(c_result.value)


        return result
    
    @property
    def p_subaggregate_array_type(
        self
    ) -> BaseTypeDecl:
        """
        If this aggregate instance is a subaggregate of a multidimensional
        array aggregate, return the overall array type.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _base_aggregate_p_subaggregate_array_type)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_subaggregate_dimension(
        self
    ) -> int:
        """
        If this aggregate instance is a subaggregate of a multidimensional
        array aggregate, return the index of its matching dimension.

        .. note:: the returned index is 0-based, where index 0 designates the
           the first dimension of the array type. However, since this property
           works on subaggregates, the returned index will necessarily always
           be greater or equal to 1.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_int(), _base_aggregate_p_subaggregate_dimension)
        result = c_result.value


        return result

    _field_names = Expr._field_names + (
        "f_ancestor_expr",
        "f_assocs",
    )







class Aggregate(BaseAggregate):
    """
    Subclass of :py:class:`BaseAggregate`.

    Aggregate that is not a ``null record`` aggregate (:rmlink:`4.3`).

    Derived nodes: :py:class:`BracketAggregate`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseAggregate._field_names + (
    )

    _kind_name = 'Aggregate'






class BracketAggregate(Aggregate):
    """
    Subclass of :py:class:`Aggregate`.

    Bracket array or container aggregate (Ada 2020, :rmlink:`4.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Aggregate._field_names + (
    )

    _kind_name = 'BracketAggregate'






class DeltaAggregate(BaseAggregate):
    """
    Subclass of :py:class:`BaseAggregate`.

    Aggregate for delta aggregate (Ada 2022, :rmlink:`4.3`).

    Derived nodes: :py:class:`BracketDeltaAggregate`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseAggregate._field_names + (
    )

    _kind_name = 'DeltaAggregate'






class BracketDeltaAggregate(DeltaAggregate):
    """
    Subclass of :py:class:`DeltaAggregate`.

    Bracket delta aggregate (Ada 2020, :rmlink:`4.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = DeltaAggregate._field_names + (
    )

    _kind_name = 'BracketDeltaAggregate'






class NullRecordAggregate(BaseAggregate):
    """
    Subclass of :py:class:`BaseAggregate`.

    Aggregate for ``null record`` (:rmlink:`4.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseAggregate._field_names + (
    )

    _kind_name = 'NullRecordAggregate'






class BinOp(Expr):
    """
    Subclass of :py:class:`Expr`.

    Binary expression.

    This encompasses several ARM expressions, because it is used for every
    binary expression in Ada, all documented in ::rmlink:`4.4`.

    Derived nodes: :py:class:`RelationOp`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_left(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_bin_op_f_left)



        return result
    
    @property
    def f_op(
        self
    ) -> Op:
        """
        This field can contain one of the following nodes:
        :py:class:`OpAndThen`, :py:class:`OpAnd`, :py:class:`OpDiv`,
        :py:class:`OpDoubleDot`, :py:class:`OpEq`, :py:class:`OpGt`,
        :py:class:`OpGte`, :py:class:`OpLt`, :py:class:`OpLte`,
        :py:class:`OpMinus`, :py:class:`OpMod`, :py:class:`OpMult`,
        :py:class:`OpNeq`, :py:class:`OpOrElse`, :py:class:`OpOr`,
        :py:class:`OpPlus`, :py:class:`OpPow`, :py:class:`OpRem`,
        :py:class:`OpXor`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_bin_op_f_op)



        return result
    
    @property
    def f_right(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`BoxExpr`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
        :py:class:`CondExpr`, :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_bin_op_f_right)



        return result

    _field_names = Expr._field_names + (
        "f_left",
        "f_op",
        "f_right",
    )

    _kind_name = 'BinOp'






class RelationOp(BinOp):
    """
    Subclass of :py:class:`BinOp`.

    Binary operation that compares two value, producing a boolean
    (:rmlink:`4.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BinOp._field_names + (
    )

    _kind_name = 'RelationOp'






class BoxExpr(Expr):
    """
    Subclass of :py:class:`Expr`.

    Box expression (``<>``).

    This is not an expression per-se in Ada, but treating it as one helps us
    keep coherent types in some cases, like aggregates expressions.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Expr._field_names + (
    )

    _kind_name = 'BoxExpr'






class CaseExprAlternative(Expr):
    """
    Subclass of :py:class:`Expr`.

    Alternative in a ``case`` expression (``when ... => ...``).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_choices(
        self
    ) -> AlternativesList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DiscreteSubtypeIndication`,
        :py:class:`DottedName`, :py:class:`ExplicitDeref`,
        :py:class:`FormatStringLiteral`, :py:class:`Identifier`,
        :py:class:`MembershipExpr`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`OthersDesignator`,
        :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_case_expr_alternative_f_choices)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_case_expr_alternative_f_expr)



        return result

    _field_names = Expr._field_names + (
        "f_choices",
        "f_expr",
    )

    _kind_name = 'CaseExprAlternative'






class ConcatOp(Expr):
    """
    Subclass of :py:class:`Expr`.

    Concatenation expression.

    Since concatenation expression can be huge in practice, this node handles
    them as a list of operands rather than a deep tree of binary operators, in
    order to avoid crashes while parsing of running name resolution on such
    huge expression.

    The purpose of this node is to replace the arbitrarily too deep tree of
    binary operators (which can lead to a stack overflow), as for example with
    ``"A & B & C & D & E"``:

    .. code::

       BinOp(
         Binop(
           BinOp(
             BinOp(A, &, B), & , C), &, D), &, E)

    by a single operator, handling a list of operands that can be processed
    without having to perform deep recursions:

    .. code::

       ConcatOp(A,
         ConcatOperand(&, B),
         ConcatOperand(&, C),
         ConcatOperand(&, D),
         ConcatOperand(&, E))

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_first_operand(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`CondExpr`, :py:class:`DeclExpr`,
        :py:class:`DottedName`, :py:class:`ExplicitDeref`,
        :py:class:`FormatStringLiteral`, :py:class:`Identifier`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_concat_op_f_first_operand)



        return result
    
    @property
    def f_other_operands(
        self
    ) -> ConcatOperandList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_concat_op_f_other_operands)



        return result
    
    @property
    def p_operands(
        self
    ) -> List[Expr]:
        """
        Return the operands of this concatenation expression
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _concat_op_p_operands)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result

    _field_names = Expr._field_names + (
        "f_first_operand",
        "f_other_operands",
    )

    _kind_name = 'ConcatOp'






class ConcatOperand(Expr):
    """
    Subclass of :py:class:`Expr`.

    A concatenation operator and its RHS operand.

    This node is used to represent the tuple ("&", operand) used by the
    ``ConcatOp`` node to store its ``other_operands`` list.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_operator(
        self
    ) -> OpConcat:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_concat_operand_f_operator)



        return result
    
    @property
    def f_operand(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`CondExpr`, :py:class:`DeclExpr`,
        :py:class:`DottedName`, :py:class:`ExplicitDeref`,
        :py:class:`FormatStringLiteral`, :py:class:`Identifier`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_concat_operand_f_operand)



        return result

    _field_names = Expr._field_names + (
        "f_operator",
        "f_operand",
    )

    _kind_name = 'ConcatOperand'






class CondExpr(Expr):
    """
    Subclass of :py:class:`Expr`.

    Base class for a conditional expressions (:rmlink:`4.5.7`).

    Derived nodes: :py:class:`CaseExpr`, :py:class:`IfExpr`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_dependent_exprs(
        self
    ) -> List[Expr]:
        """
        Return the dependent expressions for this conditional expression.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _cond_expr_p_dependent_exprs)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result

    _field_names = Expr._field_names + (
    )







class CaseExpr(CondExpr):
    """
    Subclass of :py:class:`CondExpr`.

    ``case`` expression (:rmlink:`4.5.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_case_expr_f_expr)



        return result
    
    @property
    def f_cases(
        self
    ) -> CaseExprAlternativeList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_case_expr_f_cases)



        return result

    _field_names = CondExpr._field_names + (
        "f_expr",
        "f_cases",
    )

    _kind_name = 'CaseExpr'






class IfExpr(CondExpr):
    """
    Subclass of :py:class:`CondExpr`.

    ``if`` expression (:rmlink`4.5.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_cond_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_if_expr_f_cond_expr)



        return result
    
    @property
    def f_then_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_if_expr_f_then_expr)



        return result
    
    @property
    def f_alternatives(
        self
    ) -> ElsifExprPartList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_if_expr_f_alternatives)



        return result
    
    @property
    def f_else_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_if_expr_f_else_expr)



        return result

    _field_names = CondExpr._field_names + (
        "f_cond_expr",
        "f_then_expr",
        "f_alternatives",
        "f_else_expr",
    )

    _kind_name = 'IfExpr'






class ContractCases(Expr):
    """
    Subclass of :py:class:`Expr`.

    List of associations for the ``Contract_Case`` aspect.

    Contract cases is a non standard Ada extension that's mainly useful in
    SPARK. See the SPARK RM for more details.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_contract_cases(
        self
    ) -> ContractCaseAssocList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_contract_cases_f_contract_cases)



        return result

    _field_names = Expr._field_names + (
        "f_contract_cases",
    )

    _kind_name = 'ContractCases'






class DeclExpr(Expr):
    """
    Subclass of :py:class:`Expr`.

    Declare expression (Ada 2022, :rmlink:`4.5.9`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decls(
        self
    ) -> AdaNodeList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`ObjectDecl`, :py:class:`PragmaNode`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_decl_expr_f_decls)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_decl_expr_f_expr)



        return result

    _field_names = Expr._field_names + (
        "f_decls",
        "f_expr",
    )

    _kind_name = 'DeclExpr'






class FormatStringLiteral(Expr):
    """
    Subclass of :py:class:`Expr`.

    Interpolated string expression.

    See :gnat_rm:`string-interpolation` for more details.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_opening_chunk(
        self
    ) -> FormatStringTokStart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_format_string_literal_f_opening_chunk)



        return result
    
    @property
    def f_mid_exprs(
        self
    ) -> FormatStringChunkList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_format_string_literal_f_mid_exprs)



        return result
    
    @property
    def f_trailing_expr(
        self
    ) -> FormatStringChunk:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_format_string_literal_f_trailing_expr)



        return result

    _field_names = Expr._field_names + (
        "f_opening_chunk",
        "f_mid_exprs",
        "f_trailing_expr",
    )

    _kind_name = 'FormatStringLiteral'






class MembershipExpr(Expr):
    """
    Subclass of :py:class:`Expr`.

    Represent a membership test (in/not in operators) (:rmlink:`4.4`).

    Note that we don't consider them as binary operators since multiple
    expressions on the right hand side are allowed.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_membership_expr_f_expr)



        return result
    
    @property
    def f_op(
        self
    ) -> Op:
        """
        This field can contain one of the following nodes: :py:class:`OpIn`,
        :py:class:`OpNotIn`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_membership_expr_f_op)



        return result
    
    @property
    def f_membership_exprs(
        self
    ) -> ExprAlternativesList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DiscreteSubtypeName`,
        :py:class:`DottedName`, :py:class:`ExplicitDeref`,
        :py:class:`FormatStringLiteral`, :py:class:`Identifier`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_membership_expr_f_membership_exprs)



        return result

    _field_names = Expr._field_names + (
        "f_expr",
        "f_op",
        "f_membership_exprs",
    )

    _kind_name = 'MembershipExpr'






class Name(Expr):
    """
    Subclass of :py:class:`Expr`.

    Base class for names (:rmlink:`4.1`).

    Derived nodes: :py:class:`ArraySubcomponentChoiceName`,
    :py:class:`AttributeRef`, :py:class:`CallExpr`, :py:class:`DefiningName`,
    :py:class:`DiscreteSubtypeName`, :py:class:`DottedName`,
    :py:class:`EndName`, :py:class:`ExplicitDeref`, :py:class:`QualExpr`,
    :py:class:`ReduceAttributeRef`, :py:class:`SingleTokNode`,
    :py:class:`SyntheticIdentifier`, :py:class:`TargetName`,
    :py:class:`UpdateAttributeRef`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_enclosing_defining_name(
        self
    ) -> DefiningName:
        """
        If this name is part of a defining name, return the enclosing defining
        name node.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _name_p_enclosing_defining_name)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_is_defining(
        self
    ) -> bool:
        """
        Return True if this name is part of a defining name.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_is_defining)
        result = bool(c_result.value)


        return result
    
    def p_name_is(
        self, sym: str
    ) -> bool:
        """
        Helper. Check that this name matches ``sym``.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_sym = _symbol_type.unwrap(sym, _context)

        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_name_is, unwrapped_sym)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_direct_call(
        self
    ) -> bool:
        """
        Return True iff this name represents a call to a subprogram which is
        referred by its defining name. (i.e. not through a subprogram access).
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_is_direct_call)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_access_call(
        self
    ) -> bool:
        """
        Return True iff this name represents a call to subprogram through an
        access type.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_is_access_call)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_call(
        self
    ) -> bool:
        """
        Returns True if this Name corresponds to a call.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_is_call)
        result = bool(c_result.value)


        return result
    
    def p_is_dot_call(
        self, imprecise_fallback: bool = False
    ) -> bool:
        """
        Returns True if this Name corresponds to a dot notation call.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_is_dot_call, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    def p_all_env_elements(
        self, seq: bool = True, seq_from: AdaNode = None
    ) -> List[AdaNode]:
        """
        Return all elements in self's scope that are lexically named like self.
        """
        

        

        unwrapped_seq = bool(seq)
        unwrapped_seq_from = AdaNode._unwrap(seq_from)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _name_p_all_env_elements, unwrapped_seq, unwrapped_seq_from)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_called_subp_spec(
        self
    ) -> BaseFormalParamHolder:
        """
        Return the subprogram specification of the subprogram or subprogram
        access that is being called by this exact Name, if relevant. Note that
        when inside an instantiated generic, this will return the spec of the
        actual subprogram.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _name_p_called_subp_spec)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_referenced_decl(
        self, imprecise_fallback: bool = False
    ) -> BasicDecl:
        """
        Return the declaration this node references after name resolution. If
        imprecise_fallback is True, errors raised during resolution of the xref
        equation are caught and a fallback mechanism is triggered, which tries
        to find the referenced declaration in an ad-hoc way.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _name_p_referenced_decl, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_referenced_defining_name(
        self, imprecise_fallback: bool = False
    ) -> DefiningName:
        """
        Like ``referenced_decl``, but will return the defining identifier for
        the decl, rather than the basic declaration node itself.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _name_p_referenced_defining_name, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_failsafe_referenced_decl(
        self, imprecise_fallback: bool = False
    ) -> RefdDecl:
        """
        Failsafe version of ``referenced_decl``. Returns a ``RefdDecl``, which
        can be precise, imprecise, or error.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(RefdDecl._c_type(), _name_p_failsafe_referenced_decl, unwrapped_imprecise_fallback)
        result = RefdDecl._wrap(c_result)


        return result
    
    @property
    def p_name_designated_type(
        self
    ) -> BaseTypeDecl:
        """
        Like SubtypeIndication.designated_type, but on names, since because of
        Ada's ambiguous grammar, some subtype indications will be parsed as
        names.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _name_p_name_designated_type)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_is_static_subtype(
        self, imprecise_fallback: bool = False
    ) -> bool:
        """
        Returns whether self denotes a static subtype or not.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_is_static_subtype, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    def p_name_matches(
        self, n: Name
    ) -> bool:
        """
        Return whether two names match each other.

        This compares the symbol for Identifier and StringLiteral nodes. We
        consider that there is no match for all other node kinds.
        """
        

        

        unwrapped_n = Name._unwrap(n)

        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_name_matches, unwrapped_n)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_operator_name(
        self
    ) -> bool:
        """
        Return whether the name that self designates is an operator.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_is_operator_name)
        result = bool(c_result.value)


        return result
    
    def p_is_write_reference(
        self, imprecise_fallback: bool = False
    ) -> bool:
        """
        Whether this name is a write reference.

        For example, ``X`` is a write reference in the following cases:

        1. ``X := 2;``

        2. ``X (2) := 2;``

        3. ``P(F => X)`` where F is declared ``out`` or ``in out``.

        4. ``P(F => T (X))`` where F is declared ``out`` or ``in out``

        5. ``X'Access``.

        6. ``X.C := 2``, ``R.X := 2``

        7. ``X.P`` where the formal for X is declared ``out`` or ``in out``.

        .. note:: This is an experimental feature. There might be some
           discrepancy with the GNAT concept of "write reference".
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_is_write_reference, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    def p_is_static_call(
        self, imprecise_fallback: bool = False
    ) -> bool:
        """
        Returns True if this Name corresponds to a static non-dispatching call.
        In other words, this will return True if and only if the target of the
        call is known statically.

        .. note:: This is an experimental feature. There might be some
           discrepancy with the GNAT concept of "static call".
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_is_static_call, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    @property
    def p_call_params(
        self
    ) -> List[ParamActual]:
        """
        Returns an array of pairs, associating formal parameters to actual or
        default expressions.
        """
        

        


        
        c_result = self._eval_field(_ParamActualArrayConverter.c_type(), _name_p_call_params)
        result = _ParamActualArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_relative_name(
        self
    ) -> Name:
        """
        Returns the relative name of this instance. For example, for a prefix
        ``A.B.C``, this will return ``C``.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _name_p_relative_name)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_as_symbol_array(
        self
    ) -> List[str]:
        """
        Turn this name into an array of symbols.

        For instance, a node with name ``A.B.C`` is turned into ``['A', 'B',
        'C']``.

        Only simple name kinds are allowed: Identifier, DottedName and
        DefiningName. Any other kind will trigger a PreconditionFailure.
        """
        

        


        
        c_result = self._eval_field(_UnboundedTextTypeArrayConverter.c_type(), _name_p_as_symbol_array)
        result = _UnboundedTextTypeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_canonical_text(
        self
    ) -> str:
        """
        Return a canonicalized version of this name's text.

        Only simple name kinds are allowed: Identifier, DottedName and
        DefiningName. Any other kind will trigger a PreconditionFailure.
        """
        

        


        
        c_result = self._eval_field(_symbol_type(), _name_p_canonical_text)
        result = _symbol_type.wrap(c_result)


        return result
    
    @property
    def p_is_constant(
        self
    ) -> bool:
        """
        Return whether this name denotes a constant value.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _name_p_is_constant)
        result = bool(c_result.value)


        return result

    _field_names = Expr._field_names + (
    )







class ArraySubcomponentChoiceName(Name):
    """
    Subclass of :py:class:`Name`.

    Name for an array subcomponent choice of a deep delta aggregate.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`ArraySubcomponentChoiceName`, :py:class:`DottedName`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_array_subcomponent_choice_name_f_name)



        return result
    
    @property
    def f_suffix(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`BasicAssocList`,
        :py:class:`BinOp`, :py:class:`CallExpr`, :py:class:`CharLiteral`,
        :py:class:`DiscreteSubtypeIndication`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_array_subcomponent_choice_name_f_suffix)



        return result

    _field_names = Name._field_names + (
        "f_name",
        "f_suffix",
    )

    _kind_name = 'ArraySubcomponentChoiceName'






class AttributeRef(Name):
    """
    Subclass of :py:class:`Name`.

    Expression to reference an attribute (:rmlink:`4.1.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_prefix(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_attribute_ref_f_prefix)



        return result
    
    @property
    def f_attribute(
        self
    ) -> Identifier:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_attribute_ref_f_attribute)



        return result
    
    @property
    def f_args(
        self
    ) -> AssocList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`ParamAssoc`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_attribute_ref_f_args)



        return result

    _field_names = Name._field_names + (
        "f_prefix",
        "f_attribute",
        "f_args",
    )

    _kind_name = 'AttributeRef'






class CallExpr(Name):
    """
    Subclass of :py:class:`Name`.

    Represent a syntactic call expression.

    At the semantic level, this can be either a subprogram call, an array
    subcomponent access expression, an array slice or a type conversion, all
    described in :rmlink:`4.1`, except for subprogram call statements,
    described in :rmlink:`6.4`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_call_expr_f_name)



        return result
    
    @property
    def f_suffix(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`BasicAssocList`,
        :py:class:`BinOp`, :py:class:`CallExpr`, :py:class:`CharLiteral`,
        :py:class:`DiscreteSubtypeIndication`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_call_expr_f_suffix)



        return result
    
    @property
    def p_kind(
        self
    ) -> str:
        """
        Return whether this expression is a subprogram call, an array
        subcomponent access expression, an array slice or a type conversion.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_int(), _call_expr_p_kind)
        result = CallExprKind._wrap(c_result)


        return result
    
    @property
    def p_is_array_slice(
        self
    ) -> bool:
        """
        Return whether this CallExpr is actually an access to a slice of the
        array denoted by the prefix of this CallExpr.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _call_expr_p_is_array_slice)
        result = bool(c_result.value)


        return result

    _field_names = Name._field_names + (
        "f_name",
        "f_suffix",
    )

    _kind_name = 'CallExpr'






class DefiningName(Name):
    """
    Subclass of :py:class:`Name`.

    Name that defines an entity (:rmlink:`3.1`).

    Derived nodes: :py:class:`SyntheticDefiningName`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`,
        :py:class:`SyntheticIdentifier`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_defining_name_f_name)



        return result
    
    @property
    def p_canonical_fully_qualified_name(
        self
    ) -> str:
        """
        Return a canonical representation of the fully qualified name
        corresponding to this defining name.

        See ``P_Fully_Qualified_Name`` documentation for details on how
        Libadalang handles generic instantiations.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _defining_name_p_canonical_fully_qualified_name)
        result = _String.wrap(c_result)


        return result
    
    @property
    def p_unique_identifying_name(
        self
    ) -> str:
        """
        Return a unique identifying name for this defining name, provided this
        declaration is a public declaration. In the case of subprograms, this
        will include the profile.

        See ``P_Fully_Qualified_Name`` documentation for details on how
        Libadalang handles generic instantiations.

        .. attention:: This will only return a unique name for public
           declarations. Notably, anything nested in an unnamed declare block
           won't be handled correctly.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _defining_name_p_unique_identifying_name)
        result = _String.wrap(c_result)


        return result
    
    @property
    def p_fully_qualified_name_array(
        self
    ) -> List[str]:
        """
        Return the fully qualified name corresponding to this defining name, as
        an array of symbols.

        See ``P_Fully_Qualified_Name`` documentation for details on how
        Libadalang handles generic instantiations.
        """
        

        


        
        c_result = self._eval_field(_UnboundedTextTypeArrayConverter.c_type(), _defining_name_p_fully_qualified_name_array)
        result = _UnboundedTextTypeArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_fully_qualified_name(
        self
    ) -> str:
        """
        Return the fully qualified name corresponding to this defining name.

        When navigating inside a generic, Libadalang behaves as if you were
        inside the instantiated generic. Thus, declarations inside of it are
        prefixed by the fully qualified name of the instantiation and not by
        that of the generic.

        Hence, this property returns the fully qualified name of the
        instantiation, and if one wants the fully qualified name of the generic
        (while inside an instantiation), one should call
        ``P_Get_Uninstantiated_Node`` first.

        Example:

        .. code:: ada

           generic
           package My_Generic is
              X : Integer;
           end My_Generic;

           package My_Instance is new My_Generic;

        When analyzing ``X`` inside ``My_Instance``:

        * ``X.P_Fully_Qualified_Name`` returns ``My_Instance.X``.

        * ``X.P_Get_Uninstantiated_Node.P_Fully_Qualified_Name`` returns
          ``My_Generic.X``.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _defining_name_p_fully_qualified_name)
        result = _String.wrap(c_result)


        return result
    
    @property
    def p_basic_decl(
        self
    ) -> BasicDecl:
        """
        Return this DefiningName's basic declaration, discarding internal nodes
        such as Generic*Internal wrappers.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _defining_name_p_basic_decl)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_find_refs(
        self, root: AdaNode, imprecise_fallback: bool = False
    ) -> List[RefResult]:
        """
        Find all references to this defining name in the given ``root`` and its
        children.
        """
        

        

        unwrapped_root = AdaNode._unwrap(root)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_RefResultArrayConverter.c_type(), _defining_name_p_find_refs, unwrapped_root, unwrapped_imprecise_fallback)
        result = _RefResultArrayConverter.wrap(c_result, False)


        return result
    
    def p_find_all_references(
        self, units: List[AnalysisUnit], follow_renamings: bool = False, imprecise_fallback: bool = False
    ) -> List[RefResult]:
        """
        Searches all references to this defining name in the given list of
        units.

        If ``follow_renamings`` is True, also this also includes references
        that ultimately refer to this defining name, by unwinding renaming
        clauses.
        """
        

        

        unwrapped_units = _AnalysisUnitArrayConverter.unwrap(units)
        unwrapped_follow_renamings = bool(follow_renamings)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_RefResultArrayConverter.c_type(), _defining_name_p_find_all_references, unwrapped_units.c_value, unwrapped_follow_renamings, unwrapped_imprecise_fallback)
        result = _RefResultArrayConverter.wrap(c_result, False)


        return result
    
    def p_find_all_calls(
        self, units: List[AnalysisUnit], follow_renamings: bool = False, imprecise_fallback: bool = False
    ) -> List[RefResult]:
        """
        Return the list of all possible calls to the subprogram which self is
        the defining name of.

        This will return the name corresponding to the call, excluding the
        parameters if there are any. For instance, it will return ``A`` for the
        ``A (B)`` call.

        .. note:: This does not yet support calls done inside generics.
        """
        

        

        unwrapped_units = _AnalysisUnitArrayConverter.unwrap(units)
        unwrapped_follow_renamings = bool(follow_renamings)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_RefResultArrayConverter.c_type(), _defining_name_p_find_all_calls, unwrapped_units.c_value, unwrapped_follow_renamings, unwrapped_imprecise_fallback)
        result = _RefResultArrayConverter.wrap(c_result, False)


        return result
    
    def p_next_part(
        self, imprecise_fallback: bool = False
    ) -> DefiningName:
        """
        Like ``BasicDecl.next_part_for_decl`` on a defining name
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _defining_name_p_next_part, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_previous_part(
        self, imprecise_fallback: bool = False
    ) -> DefiningName:
        """
        Like ``BasicDecl.previous_part_for_decl`` on a defining name
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _defining_name_p_previous_part, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_canonical_part(
        self, imprecise_fallback: bool = False
    ) -> DefiningName:
        """
        Like ``BasicDecl.canonical_part`` on a defining name
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _defining_name_p_canonical_part, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_most_visible_part(
        self, origin: AdaNode, imprecise_fallback: bool = False
    ) -> DefiningName:
        """
        Given an origin node and the entity represented by self, this property
        returns the most visible completion of self that can be seen by origin,
        according to Ada's visibility rules.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _defining_name_p_most_visible_part, unwrapped_origin, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_all_parts(
        self, imprecise_fallback: bool = False
    ) -> List[DefiningName]:
        """
        Return all parts that define this entity, sorted from first part to
        last part.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _defining_name_p_all_parts, unwrapped_imprecise_fallback)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result
    
    def p_get_aspect(
        self, name: str, previous_parts_only: bool = False, imprecise_fallback: bool = False
    ) -> Aspect:
        """
        Return the aspect with name ``name`` associated to entity that this
        name defines.

        Aspects are properties of entities that can be specified by the Ada
        program, either via aspect specifications, pragmas, or attributes.

        Note: by default, Libadalang will check if the aspect is defined on any
        part of the entity. However, the ``previous_parts_only`` parameter can
        be set to True to limit the search to the current entity and its
        previous parts in order to comply with visibilily rules. That way, if
        an aspect is defined on the private part of a type, calling this
        property on its corresponding public view won't return the aspect
        unlike the call on the private view.

        Moreover, since aspects can be inherited, if none was found for the
        current entity, Libadalang will also search for the aspect on the
        parents of entity (in that case the ``inherited`` field will be set to
        ``True`` in the returned result).
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)
        unwrapped_previous_parts_only = bool(previous_parts_only)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(Aspect._c_type(), _defining_name_p_get_aspect, unwrapped_name, unwrapped_previous_parts_only, unwrapped_imprecise_fallback)
        result = Aspect._wrap(c_result)


        return result
    
    def p_has_aspect(
        self, name: str, previous_parts_only: bool = False, imprecise_fallback: bool = False
    ) -> bool:
        """
        Returns whether the boolean aspect named ``name`` is set on the entity
        represented by this node.

        Note: The ``previous_parts_only`` parameter controls how aspects are
        retrieved. See ``DefiningName.get_aspect`` for more information.

        Aspects are properties of entities that can be specified by the Ada
        program, either via aspect specifications, pragmas, or attributes.

        "Aspect" is used as in RM terminology (see :rmlink:`13.1`).
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)
        unwrapped_previous_parts_only = bool(previous_parts_only)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _defining_name_p_has_aspect, unwrapped_name, unwrapped_previous_parts_only, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result
    
    def p_get_pragma(
        self, name: str
    ) -> PragmaNode:
        """
        Return the pragma with name ``name`` associated to this entity.

        Please use the ``p_get_aspect`` property instead if you are interested
        in aspects, i.e. information that can be represented by either aspect
        specification nodes, pragma nodes or attribute definition nodes.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)

        
        c_result = self._eval_field(_Entity_c_type(), _defining_name_p_get_pragma, unwrapped_name)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_get_representation_clause(
        self, name: str, imprecise_fallback: bool = False
    ) -> AttributeDefClause:
        """
        Return the representation clause associated to this entity that defines
        the given attribute name.
        """
        

        

        _context = self.unit.context._c_value
        unwrapped_name = _symbol_type.unwrap(name, _context)
        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _defining_name_p_get_representation_clause, unwrapped_name, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_get_at_clause(
        self, imprecise_fallback: bool = False
    ) -> AtClause:
        """
        Return the at clause associated to this entity.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_Entity_c_type(), _defining_name_p_get_at_clause, unwrapped_imprecise_fallback)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_get_annotations(
        self, imprecise_fallback: bool = False
    ) -> List[Aspect]:
        """
        Return all the ``Annotate`` aspects defined on this entity, both
        through pragmas and aspect specifications. For a type declaration, this
        also includes all annotations defined on its base type, when relevant
        (the field ``inherited`` will be set for those).

        The ``value`` field of each returned ``Aspect`` will be set to be the
        identifier that designates the tool which is concerned by the
        annotation.

        Note: Libadalang will look for the ``Annotate`` aspects on any part of
        the entity.
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(_AspectArrayConverter.c_type(), _defining_name_p_get_annotations, unwrapped_imprecise_fallback)
        result = _AspectArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_is_imported(
        self
    ) -> bool:
        """
        Whether this entity defined by this name is imported from another
        language.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _defining_name_p_is_imported)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_ghost_code(
        self
    ) -> bool:
        """
        Return whether the entity defined by this name is ghost or not. See
        SPARK RM 6.9.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _defining_name_p_is_ghost_code)
        result = bool(c_result.value)


        return result

    _field_names = Name._field_names + (
        "f_name",
    )

    _kind_name = 'DefiningName'






class SyntheticDefiningName(DefiningName):
    """
    Subclass of :py:class:`DefiningName`.

    Synthetic DefiningName.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = DefiningName._field_names + (
    )

    _kind_name = 'SyntheticDefiningName'






class DiscreteSubtypeName(Name):
    """
    Subclass of :py:class:`Name`.

    Subtype name for membership test expressions (:rmlink:`3.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_subtype(
        self
    ) -> DiscreteSubtypeIndication:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_discrete_subtype_name_f_subtype)



        return result

    _field_names = Name._field_names + (
        "f_subtype",
    )

    _kind_name = 'DiscreteSubtypeName'






class DottedName(Name):
    """
    Subclass of :py:class:`Name`.

    Name to select a suffix in a prefix (:rmlink:`4.1.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_prefix(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`ArraySubcomponentChoiceName`, :py:class:`AttributeRef`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_dotted_name_f_prefix)



        return result
    
    @property
    def f_suffix(
        self
    ) -> BaseId:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`Identifier`,
        :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_dotted_name_f_suffix)



        return result

    _field_names = Name._field_names + (
        "f_prefix",
        "f_suffix",
    )

    _kind_name = 'DottedName'






class EndName(Name):
    """
    Subclass of :py:class:`Name`.

    self name in ``end ...;`` syntactic constructs.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_end_name_f_name)



        return result
    
    @property
    def p_basic_decl(
        self
    ) -> BasicDecl:
        """
        Returns this EndName's basic declaration
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _end_name_p_basic_decl)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = Name._field_names + (
        "f_name",
    )

    _kind_name = 'EndName'






class ExplicitDeref(Name):
    """
    Subclass of :py:class:`Name`.

    Explicit dereference expression (``.all``) (:rmlink:`4.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_prefix(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_explicit_deref_f_prefix)



        return result

    _field_names = Name._field_names + (
        "f_prefix",
    )

    _kind_name = 'ExplicitDeref'






class QualExpr(Name):
    """
    Subclass of :py:class:`Name`.

    Qualified expression (``...'(...)``) .(:rmlink:`4.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_prefix(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_qual_expr_f_prefix)



        return result
    
    @property
    def f_suffix(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`BaseAggregate`, :py:class:`ParenExpr`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_qual_expr_f_suffix)



        return result

    _field_names = Name._field_names + (
        "f_prefix",
        "f_suffix",
    )

    _kind_name = 'QualExpr'






class ReduceAttributeRef(Name):
    """
    Subclass of :py:class:`Name`.

    Reduction expression (``Reduce`` attribute). Ada 2022, RM 4.5.10.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_prefix(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`, :py:class:`ValueSequence`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_reduce_attribute_ref_f_prefix)



        return result
    
    @property
    def f_attribute(
        self
    ) -> Identifier:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_reduce_attribute_ref_f_attribute)



        return result
    
    @property
    def f_args(
        self
    ) -> AssocList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`ParamAssoc`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_reduce_attribute_ref_f_args)



        return result

    _field_names = Name._field_names + (
        "f_prefix",
        "f_attribute",
        "f_args",
    )

    _kind_name = 'ReduceAttributeRef'






class SingleTokNode(Name):
    """
    Subclass of :py:class:`Name`.

    Base class for nodes that are made up of a single token.

    Derived nodes: :py:class:`BaseId`, :py:class:`NullLiteral`,
    :py:class:`NumLiteral`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Name._field_names + (
    )







class BaseId(SingleTokNode):
    """
    Subclass of :py:class:`SingleTokNode`.

    Base class for identifiers.

    Derived nodes: :py:class:`CharLiteral`, :py:class:`Identifier`,
    :py:class:`Op`, :py:class:`StringLiteral`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SingleTokNode._field_names + (
    )







class CharLiteral(BaseId):
    """
    Subclass of :py:class:`BaseId`.

    Character literal (:rmlink:`4.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_denoted_value(
        self
    ) -> str:
        """
        Return the value that this literal denotes.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint32(), _char_literal_p_denoted_value)
        result = chr(c_result.value)


        return result

    _field_names = BaseId._field_names + (
    )

    _kind_name = 'CharLiteral'






class Identifier(BaseId):
    """
    Subclass of :py:class:`BaseId`.

    Regular identifier (:rmlink:`2.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseId._field_names + (
    )

    _kind_name = 'Identifier'






class Op(BaseId):
    """
    Subclass of :py:class:`BaseId`.

    Operation in a binary expression.

    Note that the ARM does not consider "double_dot" ("..") as a binary
    operator, but we process it this way here anyway to keep things simple.

    Derived nodes: :py:class:`OpAbs`, :py:class:`OpAndThen`, :py:class:`OpAnd`,
    :py:class:`OpConcat`, :py:class:`OpDiv`, :py:class:`OpDoubleDot`,
    :py:class:`OpEq`, :py:class:`OpGt`, :py:class:`OpGte`, :py:class:`OpIn`,
    :py:class:`OpLt`, :py:class:`OpLte`, :py:class:`OpMinus`,
    :py:class:`OpMod`, :py:class:`OpMult`, :py:class:`OpNeq`,
    :py:class:`OpNotIn`, :py:class:`OpNot`, :py:class:`OpOrElse`,
    :py:class:`OpOr`, :py:class:`OpPlus`, :py:class:`OpPow`, :py:class:`OpRem`,
    :py:class:`OpXor`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseId._field_names + (
    )







class OpAbs(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpAbs'






class OpAnd(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpAnd'






class OpAndThen(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpAndThen'






class OpConcat(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpConcat'






class OpDiv(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpDiv'






class OpDoubleDot(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpDoubleDot'






class OpEq(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpEq'






class OpGt(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpGt'






class OpGte(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpGte'






class OpIn(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpIn'






class OpLt(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpLt'






class OpLte(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpLte'






class OpMinus(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpMinus'






class OpMod(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpMod'






class OpMult(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpMult'






class OpNeq(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpNeq'






class OpNot(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpNot'






class OpNotIn(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpNotIn'






class OpOr(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpOr'






class OpOrElse(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpOrElse'






class OpPlus(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpPlus'






class OpPow(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpPow'






class OpRem(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpRem'






class OpXor(Op):
    """
    Subclass of :py:class:`Op`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Op._field_names + (
    )

    _kind_name = 'OpXor'






class StringLiteral(BaseId):
    """
    Subclass of :py:class:`BaseId`.

    String literal (:rmlink:`2.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_denoted_value(
        self
    ) -> str:
        """
        Return the value that this literal denotes.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _string_literal_p_denoted_value)
        result = _String.wrap(c_result)


        return result

    _field_names = BaseId._field_names + (
    )

    _kind_name = 'StringLiteral'






class NullLiteral(SingleTokNode):
    """
    Subclass of :py:class:`SingleTokNode`.

    The ``null`` literal (:rmlink:`4.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SingleTokNode._field_names + (
    )

    _kind_name = 'NullLiteral'






class NumLiteral(SingleTokNode):
    """
    Subclass of :py:class:`SingleTokNode`.

    Base class for number literals (:rmlink:`2.4`).

    Derived nodes: :py:class:`IntLiteral`, :py:class:`RealLiteral`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SingleTokNode._field_names + (
    )







class IntLiteral(NumLiteral):
    """
    Subclass of :py:class:`NumLiteral`.

    Literal for an integer (:rmlink:`2.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_denoted_value(
        self
    ) -> int:
        """
        Return the value that this literal denotes.
        """
        

        


        
        c_result = self._eval_field(_big_integer.c_type(), _int_literal_p_denoted_value)
        result = _big_integer.wrap(c_result)


        return result

    _field_names = NumLiteral._field_names + (
    )

    _kind_name = 'IntLiteral'






class RealLiteral(NumLiteral):
    """
    Subclass of :py:class:`NumLiteral`.

    Literal for a real number (:rmlink:`2.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = NumLiteral._field_names + (
    )

    _kind_name = 'RealLiteral'






class SyntheticIdentifier(Name):
    """
    Subclass of :py:class:`Name`.

    Synthetic identifier.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Name._field_names + (
    )

    _kind_name = 'SyntheticIdentifier'






class TargetName(Name):
    """
    Subclass of :py:class:`Name`.

    Name for Ada 2020 ``@`` (:rmlink:`5.2.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Name._field_names + (
    )

    _kind_name = 'TargetName'






class UpdateAttributeRef(Name):
    """
    Subclass of :py:class:`Name`.

    Reference to the ``Update`` attribute, which is a non standard GNAT
    attribute.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_prefix(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_update_attribute_ref_f_prefix)



        return result
    
    @property
    def f_attribute(
        self
    ) -> Identifier:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_update_attribute_ref_f_attribute)



        return result
    
    @property
    def f_values(
        self
    ) -> BaseAggregate:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_update_attribute_ref_f_values)



        return result

    _field_names = Name._field_names + (
        "f_prefix",
        "f_attribute",
        "f_values",
    )

    _kind_name = 'UpdateAttributeRef'






class ParenExpr(Expr):
    """
    Subclass of :py:class:`Expr`.

    Parenthesized expression.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_paren_expr_f_expr)



        return result

    _field_names = Expr._field_names + (
        "f_expr",
    )

    _kind_name = 'ParenExpr'






class QuantifiedExpr(Expr):
    """
    Subclass of :py:class:`Expr`.

    Quantified expression (:rmlink:`4.5.8`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_quantifier(
        self
    ) -> Quantifier:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_quantified_expr_f_quantifier)



        return result
    
    @property
    def f_loop_spec(
        self
    ) -> ForLoopSpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_quantified_expr_f_loop_spec)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_quantified_expr_f_expr)



        return result

    _field_names = Expr._field_names + (
        "f_quantifier",
        "f_loop_spec",
        "f_expr",
    )

    _kind_name = 'QuantifiedExpr'






class RaiseExpr(Expr):
    """
    Subclass of :py:class:`Expr`.

    Expression to raise an exception (:rmlink:`4.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_exception_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_raise_expr_f_exception_name)



        return result
    
    @property
    def f_error_message(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_raise_expr_f_error_message)



        return result

    _field_names = Expr._field_names + (
        "f_exception_name",
        "f_error_message",
    )

    _kind_name = 'RaiseExpr'






class UnOp(Expr):
    """
    Subclass of :py:class:`Expr`.

    Unary expression.

    This encompasses several ARM expressions, because it is used for every
    unary operator in Ada. Those expressions are all documented in
    :rmlink:`4.4`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_op(
        self
    ) -> Op:
        """
        This field can contain one of the following nodes: :py:class:`OpAbs`,
        :py:class:`OpMinus`, :py:class:`OpNot`, :py:class:`OpPlus`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_un_op_f_op)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`CondExpr`, :py:class:`DeclExpr`,
        :py:class:`DottedName`, :py:class:`ExplicitDeref`,
        :py:class:`FormatStringLiteral`, :py:class:`Identifier`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_un_op_f_expr)



        return result

    _field_names = Expr._field_names + (
        "f_op",
        "f_expr",
    )

    _kind_name = 'UnOp'






class ForLoopIterFilter(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Represent the ``when ...`` filter after a for loop specification. This
    class has no RM existence, it is used internally to wrap the filtering
    expression, so as to have a dedicated name resolution entry point for it
    and make sure it is resolved separatly from the ``ForLoopSpec`` itself
    (which it cannot influence anyway).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_for_loop_iter_filter_f_expr)



        return result

    _field_names = AdaNode._field_names + (
        "f_expr",
    )

    _kind_name = 'ForLoopIterFilter'






class FormatStringChunk(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Chunk of a format string literal.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_format_string_chunk_f_expr)



        return result
    
    @property
    def f_string_tok(
        self
    ) -> FormatStringTokNode:
        """
        This field can contain one of the following nodes:
        :py:class:`FormatStringTokEnd`, :py:class:`FormatStringTokMid`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_format_string_chunk_f_string_tok)



        return result
    
    @property
    def p_image_subprogram(
        self
    ) -> BasicDecl:
        """
        Return the Image subprogram declaration referred by this format string
        chunk expression.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _format_string_chunk_p_image_subprogram)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = AdaNode._field_names + (
        "f_expr",
        "f_string_tok",
    )

    _kind_name = 'FormatStringChunk'






class FormatStringTokNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Node holding a format string token.

    Derived nodes: :py:class:`FormatStringTokEnd`,
    :py:class:`FormatStringTokMid`, :py:class:`FormatStringTokStart`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_denoted_value(
        self
    ) -> str:
        """
        Return the value that this literal denotes.
        """
        

        


        
        c_result = self._eval_field(_String.c_type(), _format_string_tok_node_p_denoted_value)
        result = _String.wrap(c_result)


        return result

    _field_names = AdaNode._field_names + (
    )







class FormatStringTokEnd(FormatStringTokNode):
    """
    Subclass of :py:class:`FormatStringTokNode`.

    Node holding a formatting "end" token.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = FormatStringTokNode._field_names + (
    )

    _kind_name = 'FormatStringTokEnd'






class FormatStringTokMid(FormatStringTokNode):
    """
    Subclass of :py:class:`FormatStringTokNode`.

    Node holding a formatting "middle" token.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = FormatStringTokNode._field_names + (
    )

    _kind_name = 'FormatStringTokMid'






class FormatStringTokStart(FormatStringTokNode):
    """
    Subclass of :py:class:`FormatStringTokNode`.

    Node holding a formatting "start" token.

    Derived nodes: :py:class:`FormatStringTokString`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = FormatStringTokNode._field_names + (
    )

    _kind_name = 'FormatStringTokStart'






class FormatStringTokString(FormatStringTokStart):
    """
    Subclass of :py:class:`FormatStringTokStart`.

    Node holding a formatting "string" token. This token is used when the
    corresponding interpolated string doesn't have any expression to expand.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = FormatStringTokStart._field_names + (
    )

    _kind_name = 'FormatStringTokString'






class HandledStmts(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    List of statements, with optional exception handlers (:rmlink:`11.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_stmts(
        self
    ) -> StmtList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`PragmaNode`, :py:class:`Stmt`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_handled_stmts_f_stmts)



        return result
    
    @property
    def f_exceptions(
        self
    ) -> AdaNodeList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`ExceptionHandler`, :py:class:`PragmaNode`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_handled_stmts_f_exceptions)



        return result

    _field_names = AdaNode._field_names + (
        "f_stmts",
        "f_exceptions",
    )

    _kind_name = 'HandledStmts'






class InterfaceKind(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Kind of interface type.

    Derived nodes: :py:class:`InterfaceKindLimited`,
    :py:class:`InterfaceKindProtected`, :py:class:`InterfaceKindSynchronized`,
    :py:class:`InterfaceKindTask`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class InterfaceKindLimited(InterfaceKind):
    """
    Subclass of :py:class:`InterfaceKind`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = InterfaceKind._field_names + (
    )

    _kind_name = 'InterfaceKindLimited'






class InterfaceKindProtected(InterfaceKind):
    """
    Subclass of :py:class:`InterfaceKind`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = InterfaceKind._field_names + (
    )

    _kind_name = 'InterfaceKindProtected'






class InterfaceKindSynchronized(InterfaceKind):
    """
    Subclass of :py:class:`InterfaceKind`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = InterfaceKind._field_names + (
    )

    _kind_name = 'InterfaceKindSynchronized'






class InterfaceKindTask(InterfaceKind):
    """
    Subclass of :py:class:`InterfaceKind`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = InterfaceKind._field_names + (
    )

    _kind_name = 'InterfaceKindTask'






class IterType(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Iteration type for ``for`` loops.

    Derived nodes: :py:class:`IterTypeIn`, :py:class:`IterTypeOf`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class IterTypeIn(IterType):
    """
    Subclass of :py:class:`IterType`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = IterType._field_names + (
    )

    _kind_name = 'IterTypeIn'






class IterTypeOf(IterType):
    """
    Subclass of :py:class:`IterType`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = IterType._field_names + (
    )

    _kind_name = 'IterTypeOf'






class LibraryItem(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Library item in a compilation unit (:rmlink:`10.1.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_private(
        self
    ) -> PrivateNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_library_item_f_has_private)



        return result
    
    @property
    def f_item(
        self
    ) -> BasicDecl:
        """
        This field can contain one of the following nodes:
        :py:class:`AbstractSubpDecl`, :py:class:`BaseSubpBody`,
        :py:class:`ErrorDecl`, :py:class:`GenericDecl`,
        :py:class:`GenericInstantiation`, :py:class:`GenericRenamingDecl`,
        :py:class:`PackageBody`, :py:class:`PackageDecl`,
        :py:class:`PackageRenamingDecl`, :py:class:`SubpDecl`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_library_item_f_item)



        return result

    _field_names = AdaNode._field_names + (
        "f_has_private",
        "f_item",
    )

    _kind_name = 'LibraryItem'






class LimitedNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``limited`` keyword.

    Derived nodes: :py:class:`LimitedAbsent`, :py:class:`LimitedPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _limited_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class LimitedAbsent(LimitedNode):
    """
    Subclass of :py:class:`LimitedNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = LimitedNode._field_names + (
    )

    _kind_name = 'LimitedAbsent'






class LimitedPresent(LimitedNode):
    """
    Subclass of :py:class:`LimitedNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = LimitedNode._field_names + (
    )

    _kind_name = 'LimitedPresent'






class LoopSpec(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Base class for loop specifications (:rmlink:`5.5`).

    Derived nodes: :py:class:`ForLoopSpec`, :py:class:`WhileLoopSpec`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class ForLoopSpec(LoopSpec):
    """
    Subclass of :py:class:`LoopSpec`.

    Specification for a ``for`` loop (:rmlink:`5.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_var_decl(
        self
    ) -> ForLoopVarDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_for_loop_spec_f_var_decl)



        return result
    
    @property
    def f_loop_type(
        self
    ) -> IterType:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_for_loop_spec_f_loop_type)



        return result
    
    @property
    def f_has_reverse(
        self
    ) -> ReverseNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_for_loop_spec_f_has_reverse)



        return result
    
    @property
    def f_iter_expr(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DiscreteSubtypeIndication`,
        :py:class:`DottedName`, :py:class:`ExplicitDeref`,
        :py:class:`Identifier`, :py:class:`QualExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_for_loop_spec_f_iter_expr)



        return result
    
    @property
    def f_iter_filter(
        self
    ) -> ForLoopIterFilter:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_for_loop_spec_f_iter_filter)



        return result

    _field_names = LoopSpec._field_names + (
        "f_var_decl",
        "f_loop_type",
        "f_has_reverse",
        "f_iter_expr",
        "f_iter_filter",
    )

    _kind_name = 'ForLoopSpec'






class WhileLoopSpec(LoopSpec):
    """
    Subclass of :py:class:`LoopSpec`.

    Specification for a ``while`` loop (:rmlink:`5.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_while_loop_spec_f_expr)



        return result

    _field_names = LoopSpec._field_names + (
        "f_expr",
    )

    _kind_name = 'WhileLoopSpec'






class Mode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Syntactic indicators for passing modes in formals (:rmlink:`6.1`).

    Derived nodes: :py:class:`ModeDefault`, :py:class:`ModeInOut`,
    :py:class:`ModeIn`, :py:class:`ModeOut`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class ModeDefault(Mode):
    """
    Subclass of :py:class:`Mode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Mode._field_names + (
    )

    _kind_name = 'ModeDefault'






class ModeIn(Mode):
    """
    Subclass of :py:class:`Mode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Mode._field_names + (
    )

    _kind_name = 'ModeIn'






class ModeInOut(Mode):
    """
    Subclass of :py:class:`Mode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Mode._field_names + (
    )

    _kind_name = 'ModeInOut'






class ModeOut(Mode):
    """
    Subclass of :py:class:`Mode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Mode._field_names + (
    )

    _kind_name = 'ModeOut'






class MultiAbstractStateDecl(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Node that holds several AbstractStateDecl nodes, which is necessary when
    the Abstract_State aspect is associated with an aggregate in order to
    declare a list of abstract states.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decls(
        self
    ) -> AbstractStateDeclList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`AbstractStateDecl`,
        :py:class:`ParenAbstractStateDecl`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_multi_abstract_state_decl_f_decls)



        return result

    _field_names = AdaNode._field_names + (
        "f_decls",
    )

    _kind_name = 'MultiAbstractStateDecl'






class NotNull(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``not null`` keywords.

    Derived nodes: :py:class:`NotNullAbsent`, :py:class:`NotNullPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _not_null_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class NotNullAbsent(NotNull):
    """
    Subclass of :py:class:`NotNull`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = NotNull._field_names + (
    )

    _kind_name = 'NotNullAbsent'






class NotNullPresent(NotNull):
    """
    Subclass of :py:class:`NotNull`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = NotNull._field_names + (
    )

    _kind_name = 'NotNullPresent'






class NullComponentDecl(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Placeholder for the ``null`` in lists of components (:rmlink:`3.8`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )

    _kind_name = 'NullComponentDecl'






class OthersDesignator(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    ``other`` designator.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )

    _kind_name = 'OthersDesignator'






class OverridingNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Syntactic indicators for subprogram overriding modes.

    Derived nodes: :py:class:`OverridingNotOverriding`,
    :py:class:`OverridingOverriding`, :py:class:`OverridingUnspecified`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class OverridingNotOverriding(OverridingNode):
    """
    Subclass of :py:class:`OverridingNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = OverridingNode._field_names + (
    )

    _kind_name = 'OverridingNotOverriding'






class OverridingOverriding(OverridingNode):
    """
    Subclass of :py:class:`OverridingNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = OverridingNode._field_names + (
    )

    _kind_name = 'OverridingOverriding'






class OverridingUnspecified(OverridingNode):
    """
    Subclass of :py:class:`OverridingNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = OverridingNode._field_names + (
    )

    _kind_name = 'OverridingUnspecified'






class Params(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    List of parameter specifications.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_params(
        self
    ) -> ParamSpecList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_params_f_params)



        return result

    _field_names = AdaNode._field_names + (
        "f_params",
    )

    _kind_name = 'Params'






class ParenAbstractStateDecl(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Holds an AbstractStateDecl between parentheses. Needed to support the
    syntax:

    .. code:: ada

       package Pkg
           with Abstract_State => (A, (B with Some_Aspect))

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decl(
        self
    ) -> AdaNode:
        """
        This field can contain one of the following nodes:
        :py:class:`AbstractStateDecl`, :py:class:`ParenAbstractStateDecl`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_paren_abstract_state_decl_f_decl)



        return result

    _field_names = AdaNode._field_names + (
        "f_decl",
    )

    _kind_name = 'ParenAbstractStateDecl'






class PpDirective(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Base node for all preprocessor directives.

    Derived nodes: :py:class:`PpElseDirective`, :py:class:`PpElsifDirective`,
    :py:class:`PpEndIfDirective`, :py:class:`PpIfDirective`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class PpElseDirective(PpDirective):
    """
    Subclass of :py:class:`PpDirective`.

    ``else`` preprocessor directive.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = PpDirective._field_names + (
    )

    _kind_name = 'PpElseDirective'






class PpElsifDirective(PpDirective):
    """
    Subclass of :py:class:`PpDirective`.

    ``elsif ... [then]`` preprocessor directive.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`BinOp`, :py:class:`Identifier`,
        :py:class:`ParenExpr`, :py:class:`UnOp`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_pp_elsif_directive_f_expr)



        return result
    
    @property
    def f_then_kw(
        self
    ) -> PpThenKw:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_pp_elsif_directive_f_then_kw)



        return result

    _field_names = PpDirective._field_names + (
        "f_expr",
        "f_then_kw",
    )

    _kind_name = 'PpElsifDirective'






class PpEndIfDirective(PpDirective):
    """
    Subclass of :py:class:`PpDirective`.

    ``end if;`` preprocessor directive.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = PpDirective._field_names + (
    )

    _kind_name = 'PpEndIfDirective'






class PpIfDirective(PpDirective):
    """
    Subclass of :py:class:`PpDirective`.

    ``if ... [then]`` preprocessor directive.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`BinOp`, :py:class:`Identifier`,
        :py:class:`ParenExpr`, :py:class:`UnOp`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_pp_if_directive_f_expr)



        return result
    
    @property
    def f_then_kw(
        self
    ) -> PpThenKw:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_pp_if_directive_f_then_kw)



        return result

    _field_names = PpDirective._field_names + (
        "f_expr",
        "f_then_kw",
    )

    _kind_name = 'PpIfDirective'






class PpThenKw(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    ``then`` keyword in preprocessor directives.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )

    _kind_name = 'PpThenKw'






class PragmaNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Class for pragmas (:rmlink:`2.8`). Pragmas are compiler directives, that
    can be language or compiler defined.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_id(
        self
    ) -> Identifier:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_pragma_node_f_id)



        return result
    
    @property
    def f_args(
        self
    ) -> BaseAssocList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_pragma_node_f_args)



        return result
    
    @property
    def p_is_ghost_code(
        self
    ) -> bool:
        """
        Return whether this pragma is ghost code or not. See SPARK RM 6.9.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _pragma_node_p_is_ghost_code)
        result = bool(c_result.value)


        return result
    
    @property
    def p_associated_entities(
        self
    ) -> List[DefiningName]:
        """
        Return an array of ``BasicDecl`` instances associated with this pragma,
        or an empty array if non applicable.
        """
        

        


        
        c_result = self._eval_field(_AdaNodeArrayConverter.c_type(), _pragma_node_p_associated_entities)
        result = _AdaNodeArrayConverter.wrap(c_result, False)


        return result

    _field_names = AdaNode._field_names + (
        "f_id",
        "f_args",
    )

    _kind_name = 'PragmaNode'






class PrivateNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``private`` keyword.

    Derived nodes: :py:class:`PrivateAbsent`, :py:class:`PrivatePresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _private_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class PrivateAbsent(PrivateNode):
    """
    Subclass of :py:class:`PrivateNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = PrivateNode._field_names + (
    )

    _kind_name = 'PrivateAbsent'






class PrivatePresent(PrivateNode):
    """
    Subclass of :py:class:`PrivateNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = PrivateNode._field_names + (
    )

    _kind_name = 'PrivatePresent'






class ProtectedDef(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Type definition for a protected object (:rmlink:`9.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_public_part(
        self
    ) -> PublicPart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_protected_def_f_public_part)



        return result
    
    @property
    def f_private_part(
        self
    ) -> PrivatePart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_protected_def_f_private_part)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_protected_def_f_end_name)



        return result

    _field_names = AdaNode._field_names + (
        "f_public_part",
        "f_private_part",
        "f_end_name",
    )

    _kind_name = 'ProtectedDef'






class ProtectedNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``protected`` keyword.

    Derived nodes: :py:class:`ProtectedAbsent`, :py:class:`ProtectedPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _protected_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class ProtectedAbsent(ProtectedNode):
    """
    Subclass of :py:class:`ProtectedNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ProtectedNode._field_names + (
    )

    _kind_name = 'ProtectedAbsent'






class ProtectedPresent(ProtectedNode):
    """
    Subclass of :py:class:`ProtectedNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ProtectedNode._field_names + (
    )

    _kind_name = 'ProtectedPresent'






class Quantifier(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Type for quantified expressions.

    Derived nodes: :py:class:`QuantifierAll`, :py:class:`QuantifierSome`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class QuantifierAll(Quantifier):
    """
    Subclass of :py:class:`Quantifier`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Quantifier._field_names + (
    )

    _kind_name = 'QuantifierAll'






class QuantifierSome(Quantifier):
    """
    Subclass of :py:class:`Quantifier`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Quantifier._field_names + (
    )

    _kind_name = 'QuantifierSome'






class RangeSpec(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Range specification (:rmlink:`3.5.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_range(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`BinOp`, :py:class:`BoxExpr`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_range_spec_f_range)



        return result

    _field_names = AdaNode._field_names + (
        "f_range",
    )

    _kind_name = 'RangeSpec'






class RenamingClause(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Renaming clause, used everywhere renamings are valid.

    Derived nodes: :py:class:`SyntheticRenamingClause`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_renamed_object(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_renaming_clause_f_renamed_object)



        return result

    _field_names = AdaNode._field_names + (
        "f_renamed_object",
    )

    _kind_name = 'RenamingClause'






class SyntheticRenamingClause(RenamingClause):
    """
    Subclass of :py:class:`RenamingClause`.

    Synthetic renaming clause. Used to synthesize object decls with renamings.
    (See to_anonymous_object_decl).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = RenamingClause._field_names + (
    )

    _kind_name = 'SyntheticRenamingClause'






class ReverseNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``reverse`` keyword.

    Derived nodes: :py:class:`ReverseAbsent`, :py:class:`ReversePresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _reverse_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class ReverseAbsent(ReverseNode):
    """
    Subclass of :py:class:`ReverseNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ReverseNode._field_names + (
    )

    _kind_name = 'ReverseAbsent'






class ReversePresent(ReverseNode):
    """
    Subclass of :py:class:`ReverseNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = ReverseNode._field_names + (
    )

    _kind_name = 'ReversePresent'






class SelectWhenPart(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Alternative part in a ``select`` statements block (:rmlink:`9.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_cond_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_select_when_part_f_cond_expr)



        return result
    
    @property
    def f_stmts(
        self
    ) -> StmtList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`PragmaNode`, :py:class:`Stmt`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_select_when_part_f_stmts)



        return result

    _field_names = AdaNode._field_names + (
        "f_cond_expr",
        "f_stmts",
    )

    _kind_name = 'SelectWhenPart'






class Stmt(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Bass class for statements (:rmlink:`5.1`).

    Derived nodes: :py:class:`CompositeStmt`, :py:class:`ErrorStmt`,
    :py:class:`SimpleStmt`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_is_ghost_code(
        self
    ) -> bool:
        """
        Return whether this statement is ghost code or not. See SPARK RM 6.9.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _stmt_p_is_ghost_code)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class CompositeStmt(Stmt):
    """
    Subclass of :py:class:`Stmt`.

    Base class for composite statements (:rmlink:`5.1`).

    Derived nodes: :py:class:`AcceptStmt`, :py:class:`BaseLoopStmt`,
    :py:class:`BlockStmt`, :py:class:`CaseStmt`,
    :py:class:`ExtendedReturnStmt`, :py:class:`IfStmt`, :py:class:`NamedStmt`,
    :py:class:`SelectStmt`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Stmt._field_names + (
    )







class AcceptStmt(CompositeStmt):
    """
    Subclass of :py:class:`CompositeStmt`.

    ``accept`` statement (:rmlink:`9.5.2`).

    Derived nodes: :py:class:`AcceptStmtWithStmts`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_body_decl(
        self
    ) -> AcceptStmtBody:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_accept_stmt_f_body_decl)



        return result
    
    @property
    def f_entry_index_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_accept_stmt_f_entry_index_expr)



        return result
    
    @property
    def f_params(
        self
    ) -> EntryCompletionFormalParams:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_accept_stmt_f_params)



        return result
    
    def p_corresponding_entry(
        self, origin: AdaNode = None
    ) -> EntryDecl:
        """
        Return the entry which corresponds to this accept statement.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _accept_stmt_p_corresponding_entry, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = CompositeStmt._field_names + (
        "f_body_decl",
        "f_entry_index_expr",
        "f_params",
    )

    _kind_name = 'AcceptStmt'






class AcceptStmtWithStmts(AcceptStmt):
    """
    Subclass of :py:class:`AcceptStmt`.

    Extended ``accept`` statement (:rmlink:`9.5.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_stmts(
        self
    ) -> HandledStmts:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_accept_stmt_with_stmts_f_stmts)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_accept_stmt_with_stmts_f_end_name)



        return result

    _field_names = AcceptStmt._field_names + (
        "f_stmts",
        "f_end_name",
    )

    _kind_name = 'AcceptStmtWithStmts'






class BaseLoopStmt(CompositeStmt):
    """
    Subclass of :py:class:`CompositeStmt`.

    Base class for loop statements (:rmlink:`5.5`).

    Derived nodes: :py:class:`ForLoopStmt`, :py:class:`LoopStmt`,
    :py:class:`WhileLoopStmt`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_spec(
        self
    ) -> LoopSpec:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_base_loop_stmt_f_spec)



        return result
    
    @property
    def f_stmts(
        self
    ) -> StmtList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`PragmaNode`, :py:class:`Stmt`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_base_loop_stmt_f_stmts)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_base_loop_stmt_f_end_name)



        return result

    _field_names = CompositeStmt._field_names + (
        "f_spec",
        "f_stmts",
        "f_end_name",
    )







class ForLoopStmt(BaseLoopStmt):
    """
    Subclass of :py:class:`BaseLoopStmt`.

    Statement for ``for`` loops (``for ... loop ... end loop;``)
    (:rmlink:`5.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseLoopStmt._field_names + (
    )

    _kind_name = 'ForLoopStmt'






class LoopStmt(BaseLoopStmt):
    """
    Subclass of :py:class:`BaseLoopStmt`.

    Statement for simple loops (``loop ... end loop;``) (:rmlink:`5.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseLoopStmt._field_names + (
    )

    _kind_name = 'LoopStmt'






class WhileLoopStmt(BaseLoopStmt):
    """
    Subclass of :py:class:`BaseLoopStmt`.

    Statement for ``while`` loops (``while ... loop ... end loop;``)
    (:rmlink:`5.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = BaseLoopStmt._field_names + (
    )

    _kind_name = 'WhileLoopStmt'






class BlockStmt(CompositeStmt):
    """
    Subclass of :py:class:`CompositeStmt`.

    Base class for statement blocks (:rmlink:`5.6`).

    Derived nodes: :py:class:`BeginBlock`, :py:class:`DeclBlock`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = CompositeStmt._field_names + (
    )







class BeginBlock(BlockStmt):
    """
    Subclass of :py:class:`BlockStmt`.

    Statement block with no declarative part (:rmlink:`5.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_stmts(
        self
    ) -> HandledStmts:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_begin_block_f_stmts)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_begin_block_f_end_name)



        return result

    _field_names = BlockStmt._field_names + (
        "f_stmts",
        "f_end_name",
    )

    _kind_name = 'BeginBlock'






class DeclBlock(BlockStmt):
    """
    Subclass of :py:class:`BlockStmt`.

    Statement block with a declarative part (:rmlink:`5.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decls(
        self
    ) -> DeclarativePart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_decl_block_f_decls)



        return result
    
    @property
    def f_stmts(
        self
    ) -> HandledStmts:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_decl_block_f_stmts)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_decl_block_f_end_name)



        return result

    _field_names = BlockStmt._field_names + (
        "f_decls",
        "f_stmts",
        "f_end_name",
    )

    _kind_name = 'DeclBlock'






class CaseStmt(CompositeStmt):
    """
    Subclass of :py:class:`CompositeStmt`.

    ``case`` statement (:rmlink:`5.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_case_stmt_f_expr)



        return result
    
    @property
    def f_pragmas(
        self
    ) -> PragmaNodeList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_case_stmt_f_pragmas)



        return result
    
    @property
    def f_alternatives(
        self
    ) -> CaseStmtAlternativeList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_case_stmt_f_alternatives)



        return result

    _field_names = CompositeStmt._field_names + (
        "f_expr",
        "f_pragmas",
        "f_alternatives",
    )

    _kind_name = 'CaseStmt'






class ExtendedReturnStmt(CompositeStmt):
    """
    Subclass of :py:class:`CompositeStmt`.

    Extended ``return`` statement (:rmlink:`6.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decl(
        self
    ) -> ExtendedReturnStmtObjectDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_extended_return_stmt_f_decl)



        return result
    
    @property
    def f_stmts(
        self
    ) -> HandledStmts:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_extended_return_stmt_f_stmts)



        return result

    _field_names = CompositeStmt._field_names + (
        "f_decl",
        "f_stmts",
    )

    _kind_name = 'ExtendedReturnStmt'






class IfStmt(CompositeStmt):
    """
    Subclass of :py:class:`CompositeStmt`.

    ``if`` statement block (:rmlink:`5.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_cond_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_if_stmt_f_cond_expr)



        return result
    
    @property
    def f_then_stmts(
        self
    ) -> StmtList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`PragmaNode`, :py:class:`Stmt`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_if_stmt_f_then_stmts)



        return result
    
    @property
    def f_alternatives(
        self
    ) -> ElsifStmtPartList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_if_stmt_f_alternatives)



        return result
    
    @property
    def f_else_part(
        self
    ) -> ElsePart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_if_stmt_f_else_part)



        return result

    _field_names = CompositeStmt._field_names + (
        "f_cond_expr",
        "f_then_stmts",
        "f_alternatives",
        "f_else_part",
    )

    _kind_name = 'IfStmt'






class NamedStmt(CompositeStmt):
    """
    Subclass of :py:class:`CompositeStmt`.

    Wrapper class, used for composite statements that can be named (declare
    blocks, loops). This allows to both have a BasicDecl for the named entity
    declared, and a CompositeStmt for the statement hierarchy.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decl(
        self
    ) -> NamedStmtDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_named_stmt_f_decl)



        return result
    
    @property
    def f_stmt(
        self
    ) -> CompositeStmt:
        """
        This field can contain one of the following nodes:
        :py:class:`BaseLoopStmt`, :py:class:`BlockStmt`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_named_stmt_f_stmt)



        return result

    _field_names = CompositeStmt._field_names + (
        "f_decl",
        "f_stmt",
    )

    _kind_name = 'NamedStmt'






class SelectStmt(CompositeStmt):
    """
    Subclass of :py:class:`CompositeStmt`.

    ``select`` statements block (:rmlink:`9.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_guards(
        self
    ) -> SelectWhenPartList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_select_stmt_f_guards)



        return result
    
    @property
    def f_else_part(
        self
    ) -> ElsePart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_select_stmt_f_else_part)



        return result
    
    @property
    def f_then_abort_part(
        self
    ) -> ThenAbortPart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_select_stmt_f_then_abort_part)



        return result

    _field_names = CompositeStmt._field_names + (
        "f_guards",
        "f_else_part",
        "f_then_abort_part",
    )

    _kind_name = 'SelectStmt'






class ErrorStmt(Stmt):
    """
    Subclass of :py:class:`Stmt`.

    Placeholder node for syntax errors in lists of statements.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Stmt._field_names + (
    )

    _kind_name = 'ErrorStmt'






class SimpleStmt(Stmt):
    """
    Subclass of :py:class:`Stmt`.

    Base class for simple statements (:rmlink:`5.1`).

    Derived nodes: :py:class:`AbortStmt`, :py:class:`AssignStmt`,
    :py:class:`CallStmt`, :py:class:`DelayStmt`, :py:class:`ExitStmt`,
    :py:class:`GotoStmt`, :py:class:`Label`, :py:class:`NullStmt`,
    :py:class:`RaiseStmt`, :py:class:`RequeueStmt`, :py:class:`ReturnStmt`,
    :py:class:`SimpleDeclStmt`, :py:class:`TerminateAlternative`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = Stmt._field_names + (
    )







class AbortStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    ``abort`` statement (:rmlink:`9.8`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_names(
        self
    ) -> NameList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_abort_stmt_f_names)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_names",
    )

    _kind_name = 'AbortStmt'






class AssignStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    Statement for assignments (:rmlink:`5.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_dest(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_assign_stmt_f_dest)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_assign_stmt_f_expr)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_dest",
        "f_expr",
    )

    _kind_name = 'AssignStmt'






class CallStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    Statement for entry or procedure calls (:rmlink:`6.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_call(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_call_stmt_f_call)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_call",
    )

    _kind_name = 'CallStmt'






class DelayStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    ``delay`` statement (:rmlink:`9.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_until(
        self
    ) -> UntilNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_delay_stmt_f_has_until)



        return result
    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_delay_stmt_f_expr)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_has_until",
        "f_expr",
    )

    _kind_name = 'DelayStmt'






class ExitStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    ``exit`` statement (:rmlink:`5.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_loop_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_exit_stmt_f_loop_name)



        return result
    
    @property
    def f_cond_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_exit_stmt_f_cond_expr)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_loop_name",
        "f_cond_expr",
    )

    _kind_name = 'ExitStmt'






class GotoStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    ``goto`` statement (:rmlink:`5.8`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_label_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_goto_stmt_f_label_name)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_label_name",
    )

    _kind_name = 'GotoStmt'






class Label(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    Statement to declare a code label (:rmlink:`5.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decl(
        self
    ) -> LabelDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_label_f_decl)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_decl",
    )

    _kind_name = 'Label'






class NullStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    ``null;`` statement (:rmlink:`5.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SimpleStmt._field_names + (
    )

    _kind_name = 'NullStmt'






class RaiseStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    ``raise`` statement (:rmlink:`11.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_exception_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_raise_stmt_f_exception_name)



        return result
    
    @property
    def f_error_message(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_raise_stmt_f_error_message)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_exception_name",
        "f_error_message",
    )

    _kind_name = 'RaiseStmt'






class RequeueStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    ``requeue`` statement (:rmlink:`9.5.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_call_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_requeue_stmt_f_call_name)



        return result
    
    @property
    def f_has_abort(
        self
    ) -> AbortNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_requeue_stmt_f_has_abort)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_call_name",
        "f_has_abort",
    )

    _kind_name = 'RequeueStmt'






class ReturnStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    ``return`` statement (:rmlink:`6.5`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_return_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`MembershipExpr`,
        :py:class:`NullLiteral`, :py:class:`NumLiteral`, :py:class:`ParenExpr`,
        :py:class:`QualExpr`, :py:class:`QuantifiedExpr`,
        :py:class:`RaiseExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_return_stmt_f_return_expr)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_return_expr",
    )

    _kind_name = 'ReturnStmt'






class SimpleDeclStmt(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    Statement wrapping a simple object declaration.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_decl(
        self
    ) -> ObjectDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_simple_decl_stmt_f_decl)



        return result

    _field_names = SimpleStmt._field_names + (
        "f_decl",
    )

    _kind_name = 'SimpleDeclStmt'






class TerminateAlternative(SimpleStmt):
    """
    Subclass of :py:class:`SimpleStmt`.

    ``terminate`` alternative in a ``select`` statement (:rmlink:`9.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SimpleStmt._field_names + (
    )

    _kind_name = 'TerminateAlternative'






class SubpKind(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for a subprogram kind.

    Derived nodes: :py:class:`SubpKindFunction`, :py:class:`SubpKindProcedure`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class SubpKindFunction(SubpKind):
    """
    Subclass of :py:class:`SubpKind`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SubpKind._field_names + (
    )

    _kind_name = 'SubpKindFunction'






class SubpKindProcedure(SubpKind):
    """
    Subclass of :py:class:`SubpKind`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SubpKind._field_names + (
    )

    _kind_name = 'SubpKindProcedure'






class Subunit(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Subunit (``separate``) (:rmlink:`10.1.3`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subunit_f_name)



        return result
    
    @property
    def f_body(
        self
    ) -> BodyNode:
        """
        This field can contain one of the following nodes:
        :py:class:`PackageBody`, :py:class:`ProtectedBody`,
        :py:class:`SubpBody`, :py:class:`TaskBody`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subunit_f_body)



        return result
    
    @property
    def p_body_root(
        self
    ) -> BasicDecl:
        """
        Return the body in which this subunit is rooted.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _subunit_p_body_root)
        result = AdaNode._wrap(c_result)


        return result

    _field_names = AdaNode._field_names + (
        "f_name",
        "f_body",
    )

    _kind_name = 'Subunit'






class SynchronizedNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``synchronized`` keyword.

    Derived nodes: :py:class:`SynchronizedAbsent`,
    :py:class:`SynchronizedPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _synchronized_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class SynchronizedAbsent(SynchronizedNode):
    """
    Subclass of :py:class:`SynchronizedNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SynchronizedNode._field_names + (
    )

    _kind_name = 'SynchronizedAbsent'






class SynchronizedPresent(SynchronizedNode):
    """
    Subclass of :py:class:`SynchronizedNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SynchronizedNode._field_names + (
    )

    _kind_name = 'SynchronizedPresent'






class TaggedNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``tagged`` keyword.

    Derived nodes: :py:class:`TaggedAbsent`, :py:class:`TaggedPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _tagged_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class TaggedAbsent(TaggedNode):
    """
    Subclass of :py:class:`TaggedNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = TaggedNode._field_names + (
    )

    _kind_name = 'TaggedAbsent'






class TaggedPresent(TaggedNode):
    """
    Subclass of :py:class:`TaggedNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = TaggedNode._field_names + (
    )

    _kind_name = 'TaggedPresent'






class TaskDef(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Type definition for a task type (:rmlink:`9.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_interfaces(
        self
    ) -> ParentList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_task_def_f_interfaces)



        return result
    
    @property
    def f_public_part(
        self
    ) -> PublicPart:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_task_def_f_public_part)



        return result
    
    @property
    def f_private_part(
        self
    ) -> PrivatePart:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_task_def_f_private_part)



        return result
    
    @property
    def f_end_name(
        self
    ) -> EndName:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_task_def_f_end_name)



        return result

    _field_names = AdaNode._field_names + (
        "f_interfaces",
        "f_public_part",
        "f_private_part",
        "f_end_name",
    )

    _kind_name = 'TaskDef'






class ThenAbortPart(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    ``then abort`` part in a ``select`` statement block

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_stmts(
        self
    ) -> StmtList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`PragmaNode`, :py:class:`Stmt`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_then_abort_part_f_stmts)



        return result

    _field_names = AdaNode._field_names + (
        "f_stmts",
    )

    _kind_name = 'ThenAbortPart'






class TypeAttributesRepository(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Synthetic node that contains the lazy fields for the attribute subprograms
    of a given type. The lazy fields are not directly on the BaseTypeDecl node
    itself to minimize its size in memory: with this indirection, a type for
    which no function attribute is ever synthesized will not waste any memory.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )

    _kind_name = 'TypeAttributesRepository'






class TypeDef(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Base class for type definitions (:rmlink:`3.2.1`).

    Derived nodes: :py:class:`AccessDef`, :py:class:`ArrayTypeDef`,
    :py:class:`DerivedTypeDef`, :py:class:`EnumTypeDef`,
    :py:class:`FormalDiscreteTypeDef`, :py:class:`InterfaceTypeDef`,
    :py:class:`ModIntTypeDef`, :py:class:`PrivateTypeDef`,
    :py:class:`RealTypeDef`, :py:class:`RecordTypeDef`,
    :py:class:`SignedIntTypeDef`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class AccessDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Base class for access type definitions (:rmlink:`3.10`).

    Derived nodes: :py:class:`AccessToSubpDef`, :py:class:`BaseTypeAccessDef`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_not_null(
        self
    ) -> NotNull:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_access_def_f_has_not_null)



        return result

    _field_names = TypeDef._field_names + (
        "f_has_not_null",
    )







class AccessToSubpDef(AccessDef):
    """
    Subclass of :py:class:`AccessDef`.

    Type definition for accesses to subprograms (:rmlink:`3.10`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_protected(
        self
    ) -> ProtectedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_access_to_subp_def_f_has_protected)



        return result
    
    @property
    def f_subp_spec(
        self
    ) -> SubpSpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_access_to_subp_def_f_subp_spec)



        return result

    _field_names = AccessDef._field_names + (
        "f_has_protected",
        "f_subp_spec",
    )

    _kind_name = 'AccessToSubpDef'






class BaseTypeAccessDef(AccessDef):
    """
    Subclass of :py:class:`AccessDef`.

    Base class for access type definitions (:rmlink:`3.10`).

    Derived nodes: :py:class:`AnonymousTypeAccessDef`,
    :py:class:`TypeAccessDef`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AccessDef._field_names + (
    )







class AnonymousTypeAccessDef(BaseTypeAccessDef):
    """
    Subclass of :py:class:`BaseTypeAccessDef`.

    Synthetic type access, that will directly reference a type decl. It is used
    to generate synthetic anonymous access types.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_type_decl(
        self
    ) -> BaseTypeDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_anonymous_type_access_def_f_type_decl)



        return result

    _field_names = BaseTypeAccessDef._field_names + (
        "f_type_decl",
    )

    _kind_name = 'AnonymousTypeAccessDef'






class TypeAccessDef(BaseTypeAccessDef):
    """
    Subclass of :py:class:`BaseTypeAccessDef`.

    Syntactic type definition for accesses.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_all(
        self
    ) -> AllNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_type_access_def_f_has_all)



        return result
    
    @property
    def f_has_constant(
        self
    ) -> ConstantNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_type_access_def_f_has_constant)



        return result
    
    @property
    def f_subtype_indication(
        self
    ) -> SubtypeIndication:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_type_access_def_f_subtype_indication)



        return result

    _field_names = BaseTypeAccessDef._field_names + (
        "f_has_all",
        "f_has_constant",
        "f_subtype_indication",
    )

    _kind_name = 'TypeAccessDef'






class ArrayTypeDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Type definition for an array (:rmlink:`3.6`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_indices(
        self
    ) -> ArrayIndices:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_array_type_def_f_indices)



        return result
    
    @property
    def f_component_type(
        self
    ) -> ComponentDef:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_array_type_def_f_component_type)



        return result

    _field_names = TypeDef._field_names + (
        "f_indices",
        "f_component_type",
    )

    _kind_name = 'ArrayTypeDef'






class DerivedTypeDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Type definition for a derived type (:rmlink:`3.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_abstract(
        self
    ) -> AbstractNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_derived_type_def_f_has_abstract)



        return result
    
    @property
    def f_has_limited(
        self
    ) -> LimitedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_derived_type_def_f_has_limited)



        return result
    
    @property
    def f_has_synchronized(
        self
    ) -> SynchronizedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_derived_type_def_f_has_synchronized)



        return result
    
    @property
    def f_subtype_indication(
        self
    ) -> SubtypeIndication:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_derived_type_def_f_subtype_indication)



        return result
    
    @property
    def f_interfaces(
        self
    ) -> ParentList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_derived_type_def_f_interfaces)



        return result
    
    @property
    def f_record_extension(
        self
    ) -> BaseRecordDef:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_derived_type_def_f_record_extension)



        return result
    
    @property
    def f_has_with_private(
        self
    ) -> WithPrivate:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_derived_type_def_f_has_with_private)



        return result

    _field_names = TypeDef._field_names + (
        "f_has_abstract",
        "f_has_limited",
        "f_has_synchronized",
        "f_subtype_indication",
        "f_interfaces",
        "f_record_extension",
        "f_has_with_private",
    )

    _kind_name = 'DerivedTypeDef'






class EnumTypeDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Type definition for enumerations (:rmlink:`3.5.1`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_enum_literals(
        self
    ) -> EnumLiteralDeclList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_enum_type_def_f_enum_literals)



        return result

    _field_names = TypeDef._field_names + (
        "f_enum_literals",
    )

    _kind_name = 'EnumTypeDef'






class FormalDiscreteTypeDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Type definition for discrete types in generic formals (:rmlink:`12.5.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = TypeDef._field_names + (
    )

    _kind_name = 'FormalDiscreteTypeDef'






class InterfaceTypeDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Type definition for an interface (:rmlink:`3.9.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_interface_kind(
        self
    ) -> InterfaceKind:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_interface_type_def_f_interface_kind)



        return result
    
    @property
    def f_interfaces(
        self
    ) -> ParentList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_interface_type_def_f_interfaces)



        return result

    _field_names = TypeDef._field_names + (
        "f_interface_kind",
        "f_interfaces",
    )

    _kind_name = 'InterfaceTypeDef'






class ModIntTypeDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Type definition for a modular integer type (:rmlink:`3.5.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_expr(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`BoxExpr`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
        :py:class:`CondExpr`, :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_mod_int_type_def_f_expr)



        return result

    _field_names = TypeDef._field_names + (
        "f_expr",
    )

    _kind_name = 'ModIntTypeDef'






class PrivateTypeDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Type definition for a private type.

    Libadalang diverges from the ARM here, treating private types like regular
    type declarations that have an embedded type definition. This type
    definition hence corresponds to :rmlink:`7.3`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_abstract(
        self
    ) -> AbstractNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_private_type_def_f_has_abstract)



        return result
    
    @property
    def f_has_tagged(
        self
    ) -> TaggedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_private_type_def_f_has_tagged)



        return result
    
    @property
    def f_has_limited(
        self
    ) -> LimitedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_private_type_def_f_has_limited)



        return result

    _field_names = TypeDef._field_names + (
        "f_has_abstract",
        "f_has_tagged",
        "f_has_limited",
    )

    _kind_name = 'PrivateTypeDef'






class RealTypeDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Type definition for real numbers (:rmlink:`3.5.6`).

    Derived nodes: :py:class:`DecimalFixedPointDef`,
    :py:class:`FloatingPointDef`, :py:class:`OrdinaryFixedPointDef`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = TypeDef._field_names + (
    )







class DecimalFixedPointDef(RealTypeDef):
    """
    Subclass of :py:class:`RealTypeDef`.

    Type definition for decimal fixed-point numbers (:rmlink:`3.5.9`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_delta(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`BoxExpr`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
        :py:class:`CondExpr`, :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_decimal_fixed_point_def_f_delta)



        return result
    
    @property
    def f_digits(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`BoxExpr`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
        :py:class:`CondExpr`, :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_decimal_fixed_point_def_f_digits)



        return result
    
    @property
    def f_range(
        self
    ) -> RangeSpec:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_decimal_fixed_point_def_f_range)



        return result

    _field_names = RealTypeDef._field_names + (
        "f_delta",
        "f_digits",
        "f_range",
    )

    _kind_name = 'DecimalFixedPointDef'






class FloatingPointDef(RealTypeDef):
    """
    Subclass of :py:class:`RealTypeDef`.

    Type definition for floating-point numbers (:rmlink:`3.5.7`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_num_digits(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`BoxExpr`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
        :py:class:`CondExpr`, :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_floating_point_def_f_num_digits)



        return result
    
    @property
    def f_range(
        self
    ) -> RangeSpec:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_floating_point_def_f_range)



        return result

    _field_names = RealTypeDef._field_names + (
        "f_num_digits",
        "f_range",
    )

    _kind_name = 'FloatingPointDef'






class OrdinaryFixedPointDef(RealTypeDef):
    """
    Subclass of :py:class:`RealTypeDef`.

    Type definition for ordinary fixed-point numbers (:rmlink:`3.5.9`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_delta(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`BoxExpr`,
        :py:class:`CallExpr`, :py:class:`CharLiteral`, :py:class:`ConcatOp`,
        :py:class:`CondExpr`, :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_ordinary_fixed_point_def_f_delta)



        return result
    
    @property
    def f_range(
        self
    ) -> RangeSpec:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_ordinary_fixed_point_def_f_range)



        return result

    _field_names = RealTypeDef._field_names + (
        "f_delta",
        "f_range",
    )

    _kind_name = 'OrdinaryFixedPointDef'






class RecordTypeDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Type definition for a record (:rmlink:`3.8`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_abstract(
        self
    ) -> AbstractNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_record_type_def_f_has_abstract)



        return result
    
    @property
    def f_has_tagged(
        self
    ) -> TaggedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_record_type_def_f_has_tagged)



        return result
    
    @property
    def f_has_limited(
        self
    ) -> LimitedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_record_type_def_f_has_limited)



        return result
    
    @property
    def f_record_def(
        self
    ) -> BaseRecordDef:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_record_type_def_f_record_def)



        return result

    _field_names = TypeDef._field_names + (
        "f_has_abstract",
        "f_has_tagged",
        "f_has_limited",
        "f_record_def",
    )

    _kind_name = 'RecordTypeDef'






class SignedIntTypeDef(TypeDef):
    """
    Subclass of :py:class:`TypeDef`.

    Type definition for a signed integer type (:rmlink:`3.5.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_range(
        self
    ) -> RangeSpec:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_signed_int_type_def_f_range)



        return result

    _field_names = TypeDef._field_names + (
        "f_range",
    )

    _kind_name = 'SignedIntTypeDef'






class TypeExpr(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    A type expression is an abstract node that embodies the concept of a
    reference to a type.

    Since Ada has both subtype_indications and anonymous (inline) type
    declarations, a type expression contains one or the other.

    This node has no ARM correspondence.

    Derived nodes: :py:class:`AnonymousType`, :py:class:`EnumLitSynthTypeExpr`,
    :py:class:`SubtypeIndication`, :py:class:`SyntheticTypeExpr`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_type_name(
        self
    ) -> Name:
        """
        Return the name node for this type expression, if applicable, else null
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _type_expr_p_type_name)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_designated_type_decl(
        self
    ) -> BaseTypeDecl:
        """
        Returns the type declaration designated by this type expression.
        """
        

        


        
        c_result = self._eval_field(_Entity_c_type(), _type_expr_p_designated_type_decl)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_designated_type_decl_from(
        self, origin_node: AdaNode
    ) -> BaseTypeDecl:
        """
        Return the type declaration designated by this type expression as
        viewed from the node given by origin_node.
        """
        

        

        unwrapped_origin_node = AdaNode._unwrap(origin_node)

        
        c_result = self._eval_field(_Entity_c_type(), _type_expr_p_designated_type_decl_from, unwrapped_origin_node)
        result = AdaNode._wrap(c_result)


        return result
    
    def p_subtype_constraint(
        self, origin: AdaNode = None
    ) -> Constraint:
        """
        Return the constraint that this type expression defines on its
        designated subtype, if any.

        ``Origin``: Origin for this property's request. See :ref:`The origin
        parameter` for more details.
        """
        

        

        unwrapped_origin = AdaNode._unwrap(origin)

        
        c_result = self._eval_field(_Entity_c_type(), _type_expr_p_subtype_constraint, unwrapped_origin)
        result = AdaNode._wrap(c_result)


        return result
    
    @property
    def p_discriminant_constraints(
        self
    ) -> List[ParamActual]:
        """
        If this type expression designates a constrained discriminated type,
        return an array of pairs, associating each discriminant to its actual
        or default expression.
        """
        

        


        
        c_result = self._eval_field(_ParamActualArrayConverter.c_type(), _type_expr_p_discriminant_constraints)
        result = _ParamActualArrayConverter.wrap(c_result, False)


        return result
    
    @property
    def p_is_definite_subtype(
        self
    ) -> bool:
        """
        Returns whether this designates a definite subtype.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _type_expr_p_is_definite_subtype)
        result = bool(c_result.value)


        return result
    
    @property
    def p_is_statically_constrained(
        self
    ) -> bool:
        """
        Returns whether this designates a statically constrained subtype.
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _type_expr_p_is_statically_constrained)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class AnonymousType(TypeExpr):
    """
    Subclass of :py:class:`TypeExpr`.

    Container for inline anonymous array and access types declarations.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_type_decl(
        self
    ) -> AnonymousTypeDecl:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_anonymous_type_f_type_decl)



        return result

    _field_names = TypeExpr._field_names + (
        "f_type_decl",
    )

    _kind_name = 'AnonymousType'






class EnumLitSynthTypeExpr(TypeExpr):
    """
    Subclass of :py:class:`TypeExpr`.

    Synthetic node. Represents the type expression for an enum literal.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = TypeExpr._field_names + (
    )

    _kind_name = 'EnumLitSynthTypeExpr'






class SubtypeIndication(TypeExpr):
    """
    Subclass of :py:class:`TypeExpr`.

    Reference to a type by name (:rmlink:`3.2.2`).

    Derived nodes: :py:class:`ConstrainedSubtypeIndication`,
    :py:class:`DiscreteSubtypeIndication`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_not_null(
        self
    ) -> NotNull:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subtype_indication_f_has_not_null)



        return result
    
    @property
    def f_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CharLiteral`,
        :py:class:`DottedName`, :py:class:`Identifier`,
        :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_subtype_indication_f_name)



        return result
    
    @property
    def f_constraint(
        self
    ) -> Constraint:
        """
        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_subtype_indication_f_constraint)



        return result
    
    def p_is_static_subtype(
        self, imprecise_fallback: bool = False
    ) -> bool:
        """
        Returns whether self denotes a static subtype or not (i.e. determinable
        at compile time, see :rmlink:`4.9`).
        """
        

        

        unwrapped_imprecise_fallback = bool(imprecise_fallback)

        
        c_result = self._eval_field(ctypes.c_uint8(), _subtype_indication_p_is_static_subtype, unwrapped_imprecise_fallback)
        result = bool(c_result.value)


        return result

    _field_names = TypeExpr._field_names + (
        "f_has_not_null",
        "f_name",
        "f_constraint",
    )

    _kind_name = 'SubtypeIndication'






class ConstrainedSubtypeIndication(SubtypeIndication):
    """
    Subclass of :py:class:`SubtypeIndication`.

    Reference to a type with a range constraint.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SubtypeIndication._field_names + (
    )

    _kind_name = 'ConstrainedSubtypeIndication'






class DiscreteSubtypeIndication(SubtypeIndication):
    """
    Subclass of :py:class:`SubtypeIndication`.

    Reference to a type with a general constraint.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = SubtypeIndication._field_names + (
    )

    _kind_name = 'DiscreteSubtypeIndication'






class SyntheticTypeExpr(TypeExpr):
    """
    Subclass of :py:class:`TypeExpr`.

    Synthetic type expression. The designated type is already known at
    instantiation time and is to be given in the ``target_type`` field.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = TypeExpr._field_names + (
    )

    _kind_name = 'SyntheticTypeExpr'






class UnconstrainedArrayIndex(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    List of unconstrained array indexes.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_subtype_name(
        self
    ) -> Name:
        """
        This field can contain one of the following nodes:
        :py:class:`AttributeRef`, :py:class:`CharLiteral`,
        :py:class:`DottedName`, :py:class:`Identifier`,
        :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_unconstrained_array_index_f_subtype_name)



        return result
    
    @property
    def f_lower_bound(
        self
    ) -> Expr:
        """
        This field can contain one of the following nodes:
        :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`FormatStringLiteral`,
        :py:class:`Identifier`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        This field may be null even when there are no parsing errors.
        """
        

        

        result = self._eval_astnode_field(_unconstrained_array_index_f_lower_bound)



        return result

    _field_names = AdaNode._field_names + (
        "f_subtype_name",
        "f_lower_bound",
    )

    _kind_name = 'UnconstrainedArrayIndex'






class UntilNode(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``until`` keyword.

    Derived nodes: :py:class:`UntilAbsent`, :py:class:`UntilPresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _until_node_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class UntilAbsent(UntilNode):
    """
    Subclass of :py:class:`UntilNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = UntilNode._field_names + (
    )

    _kind_name = 'UntilAbsent'






class UntilPresent(UntilNode):
    """
    Subclass of :py:class:`UntilNode`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = UntilNode._field_names + (
    )

    _kind_name = 'UntilPresent'






class UseClause(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Base class for use clauses (:rmlink:`10.1.2`).

    Derived nodes: :py:class:`UsePackageClause`, :py:class:`UseTypeClause`
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = AdaNode._field_names + (
    )







class UsePackageClause(UseClause):
    """
    Subclass of :py:class:`UseClause`.

    Use clause for packages (:rmlink:`8.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_packages(
        self
    ) -> NameList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_use_package_clause_f_packages)



        return result

    _field_names = UseClause._field_names + (
        "f_packages",
    )

    _kind_name = 'UsePackageClause'






class UseTypeClause(UseClause):
    """
    Subclass of :py:class:`UseClause`.

    Use clause for types (:rmlink:`8.4`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_all(
        self
    ) -> AllNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_use_type_clause_f_has_all)



        return result
    
    @property
    def f_types(
        self
    ) -> NameList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`AttributeRef`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`ExplicitDeref`, :py:class:`Identifier`,
        :py:class:`QualExpr`, :py:class:`ReduceAttributeRef`,
        :py:class:`StringLiteral`, :py:class:`TargetName`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_use_type_clause_f_types)



        return result

    _field_names = UseClause._field_names + (
        "f_has_all",
        "f_types",
    )

    _kind_name = 'UseTypeClause'






class ValueSequence(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    The value sequence of a reduction expression (see ``ReduceAttributeRef``).
    Ada 2022, RM 4.5.10.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_iter_assoc(
        self
    ) -> IteratedAssoc:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_value_sequence_f_iter_assoc)



        return result

    _field_names = AdaNode._field_names + (
        "f_iter_assoc",
    )

    _kind_name = 'ValueSequence'






class Variant(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Single variant in a discriminated type record declaration.

    This corresponds to a ``when ... => ...`` section in a variant part.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_choices(
        self
    ) -> AlternativesList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`Allocator`, :py:class:`AttributeRef`,
        :py:class:`BaseAggregate`, :py:class:`BinOp`, :py:class:`CallExpr`,
        :py:class:`CharLiteral`, :py:class:`ConcatOp`, :py:class:`CondExpr`,
        :py:class:`DeclExpr`, :py:class:`DiscreteSubtypeIndication`,
        :py:class:`DottedName`, :py:class:`ExplicitDeref`,
        :py:class:`FormatStringLiteral`, :py:class:`Identifier`,
        :py:class:`MembershipExpr`, :py:class:`NullLiteral`,
        :py:class:`NumLiteral`, :py:class:`OthersDesignator`,
        :py:class:`ParenExpr`, :py:class:`QualExpr`,
        :py:class:`QuantifiedExpr`, :py:class:`RaiseExpr`,
        :py:class:`ReduceAttributeRef`, :py:class:`StringLiteral`,
        :py:class:`TargetName`, :py:class:`UnOp`,
        :py:class:`UpdateAttributeRef`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_variant_f_choices)



        return result
    
    @property
    def f_components(
        self
    ) -> ComponentList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_variant_f_components)



        return result

    _field_names = AdaNode._field_names + (
        "f_choices",
        "f_components",
    )

    _kind_name = 'Variant'






class VariantPart(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Variant part in a discriminated type record declaration (:rmlink:`3.8.1`).

    This corresponds to the whole ``case ... is ... end case;`` block.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_discr_name(
        self
    ) -> Identifier:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_variant_part_f_discr_name)



        return result
    
    @property
    def f_variant(
        self
    ) -> VariantList:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_variant_part_f_variant)



        return result

    _field_names = AdaNode._field_names + (
        "f_discr_name",
        "f_variant",
    )

    _kind_name = 'VariantPart'






class WithClause(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    With clause (:rmlink:`10.1.2`).

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def f_has_limited(
        self
    ) -> LimitedNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_with_clause_f_has_limited)



        return result
    
    @property
    def f_has_private(
        self
    ) -> PrivateNode:
        """
        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_with_clause_f_has_private)



        return result
    
    @property
    def f_packages(
        self
    ) -> NameList:
        """
        This field contains a list that itself contains one of the following
        nodes: :py:class:`CharLiteral`, :py:class:`DottedName`,
        :py:class:`Identifier`, :py:class:`StringLiteral`

        When there are no parsing errors, this field is never null.
        """
        

        

        result = self._eval_astnode_field(_with_clause_f_packages)



        return result

    _field_names = AdaNode._field_names + (
        "f_has_limited",
        "f_has_private",
        "f_packages",
    )

    _kind_name = 'WithClause'






class WithPrivate(AdaNode):
    """
    Subclass of :py:class:`AdaNode`.

    Qualifier for the ``private`` keyword in ``with private`` record clauses.

    Derived nodes: :py:class:`WithPrivateAbsent`,
    :py:class:`WithPrivatePresent`
    """
    __slots__ : Tuple[str, ...] = ()

    

    
    @property
    def p_as_bool(
        self
    ) -> bool:
        """
        Return whether this node is present
        """
        

        


        
        c_result = self._eval_field(ctypes.c_uint8(), _with_private_p_as_bool)
        result = bool(c_result.value)


        return result

    _field_names = AdaNode._field_names + (
    )







class WithPrivateAbsent(WithPrivate):
    """
    Subclass of :py:class:`WithPrivate`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = WithPrivate._field_names + (
    )

    _kind_name = 'WithPrivateAbsent'






class WithPrivatePresent(WithPrivate):
    """
    Subclass of :py:class:`WithPrivate`.

    This node type has no derivation.
    """
    __slots__ : Tuple[str, ...] = ()

    


    _field_names = WithPrivate._field_names + (
    )

    _kind_name = 'WithPrivatePresent'






class _EnvRebindingsType_c_type(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint64)]


_EnvRebindings_c_type = _hashable_c_pointer(_EnvRebindingsType_c_type)




class _BaseStruct:
    """
    Mixin for Ada struct wrappers.
    """

    # Subclasses will override this to a subclass of ctypes.Structure
    _c_type: ClassVar[ctypes.Structure]

    def __getitem__(self, key: int) -> Any:
        if not isinstance(key, int):
            raise TypeError(
               'Tuples items are indexed by integers, not {}'.format(type(key))
            )

        fields = self._c_type._fields_
        if 0 <= key < len(fields):
            field_name, _ = fields[key]
            return getattr(self, field_name)
        else:
            raise IndexError('There is no {}th field'.format(key))

    def __repr__(self) -> str:
        field_names = [
            name
            for name, _ in self._c_type._fields_
            if hasattr(self, name)
        ]
        if field_names:
            fields_suffix = (
                " "
                + " ".join(
                    "{}={}".format(name, getattr(self, name))
                    for name in field_names
                )
            )
        else:
            fields_suffix = ""
        return "<{}{}>".format(type(self).__name__, fields_suffix)

    @property
    def as_tuple(self) -> tuple:
        return tuple(getattr(self, f) for f, _ in self._c_type._fields_)

    def __eq__(self, other: Any) -> bool:
        return (isinstance(other, type(self)) and
                self.as_tuple == other.as_tuple)

    def __ne__(self, other: Any) -> bool:
        return not (self == other)

    def __hash__(self) -> int:
        return hash(self.as_tuple)




class _Metadata_c_type(ctypes.Structure):
    _fields_: ClassVar[List[Tuple[str, Any]]] = (
         [
        ('dottable_subp',
            ctypes.c_uint8
         ),
        ('primitive',
            AdaNode._node_c_type
         ),
        ('primitive_real_type',
            AdaNode._node_c_type
         ),
] 
    )
    _null_value: ClassVar[_Metadata_c_type]

    @property
    def as_tuple(self):
        return tuple(getattr(self, f) for f, _ in self._fields_)

    def __eq__(self, other):
        return (isinstance(other, type(self)) and
                self.as_tuple == other.as_tuple)

    def __ne__(self, other):
        return not (self == other)

    def __hash__(self):
        return hash(self.as_tuple)
class _EntityInfo_c_type(ctypes.Structure):
    _fields_: ClassVar[List[Tuple[str, Any]]] = (
         [
        ('md',
            _Metadata_c_type
         ),
        ('rebindings',
            _EnvRebindings_c_type
         ),
        ('from_rebound',
            ctypes.c_uint8
         ),
] 
    )
    _null_value: ClassVar[_EntityInfo_c_type]
class _Entity_c_type(ctypes.Structure):
    _fields_: ClassVar[List[Tuple[str, Any]]] = (
         [
        ('node',
            AdaNode._node_c_type
         ),
        ('info',
            _EntityInfo_c_type
         ),
] 
    )
    _null_value: ClassVar[_Entity_c_type]

    @classmethod
    def from_bare_node(cls, node_c_value):
        return cls(node_c_value, _EntityInfo_c_type._null_value)




class Aspect(_BaseStruct):
    """
    Composite field representing the aspect of an entity (:rmlink:`13`).
    """

    

    __slots__ = ('_exists', '_node', '_value', '_inherited')

    def __init__(
        self,
        exists: bool,
        node: AdaNode,
        value: Expr,
        inherited: bool,
    ):
        self._exists = exists
        self._node = node
        self._value = value
        self._inherited = inherited


    @property
    def exists(self) -> bool:
        """
        Whether the aspect is defined or not
        """
        return self._exists

    @property
    def node(self) -> AdaNode:
        """
        Syntactic node that defines the aspect
        """
        return self._node

    @property
    def value(self) -> Expr:
        """
        Expr node defining the value of the aspect
        """
        return self._value

    @property
    def inherited(self) -> bool:
        """
        Whether the aspect is inherited (it has been defined by a parent)
        """
        return self._inherited

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('exists',
            ctypes.c_uint8
         ),
        ('node',
            _Entity_c_type
         ),
        ('value',
            _Entity_c_type
         ),
        ('inherited',
            ctypes.c_uint8
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            bool(c_value.exists),
            AdaNode._wrap(c_value.node),
            AdaNode._wrap(c_value.value),
            bool(c_value.inherited),
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        exists = bool(value.exists)
        
        node = AdaNode._unwrap(value.node)
        
        value = Expr._unwrap(value.value)
        
        inherited = bool(value.inherited)

        result = cls._Holder(cls._c_type(
            
            exists=exists,
            
            node=node,
            
            value=value,
            
            inherited=inherited,
        ))


        return result







class CompletionItem(_BaseStruct):
    """

    """

    

    __slots__ = ('_decl', '_is_dot_call', '_is_visible', '_weight')

    def __init__(
        self,
        decl: BasicDecl,
        is_dot_call: bool,
        is_visible: bool,
        weight: int,
    ):
        self._decl = decl
        self._is_dot_call = is_dot_call
        self._is_visible = is_visible
        self._weight = weight


    @property
    def decl(self) -> BasicDecl:
        """

        """
        return self._decl

    @property
    def is_dot_call(self) -> bool:
        """

        """
        return self._is_dot_call

    @property
    def is_visible(self) -> bool:
        """

        """
        return self._is_visible

    @property
    def weight(self) -> int:
        """
        The higher the weight, the more relevant the completion item is
        """
        return self._weight

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('decl',
            _Entity_c_type
         ),
        ('is_dot_call',
            ctypes.c_uint8
         ),
        ('is_visible',
            ctypes.c_uint8
         ),
        ('weight',
            ctypes.c_int
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            AdaNode._wrap(c_value.decl),
            bool(c_value.is_dot_call),
            bool(c_value.is_visible),
            c_value.weight,
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        decl = BasicDecl._unwrap(value.decl)
        
        is_dot_call = bool(value.is_dot_call)
        
        is_visible = bool(value.is_visible)
        
        weight = int(value.weight)

        result = cls._Holder(cls._c_type(
            
            decl=decl,
            
            is_dot_call=is_dot_call,
            
            is_visible=is_visible,
            
            weight=weight,
        ))


        return result







class DiscreteRange(_BaseStruct):
    """
    Represent the range of a discrete type or subtype. The bounds are not
    evaluated, you need to call ``eval_as_int`` on them, if they're static, to
    get their value.
    """

    

    __slots__ = ('_range_type', '_low_bound', '_high_bound')

    def __init__(
        self,
        range_type: BaseTypeDecl,
        low_bound: Expr,
        high_bound: Expr,
    ):
        self._range_type = range_type
        self._low_bound = low_bound
        self._high_bound = high_bound


    @property
    def range_type(self) -> BaseTypeDecl:
        """

        """
        return self._range_type

    @property
    def low_bound(self) -> Expr:
        """

        """
        return self._low_bound

    @property
    def high_bound(self) -> Expr:
        """

        """
        return self._high_bound

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('range_type',
            _Entity_c_type
         ),
        ('low_bound',
            _Entity_c_type
         ),
        ('high_bound',
            _Entity_c_type
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            AdaNode._wrap(c_value.range_type),
            AdaNode._wrap(c_value.low_bound),
            AdaNode._wrap(c_value.high_bound),
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        range_type = BaseTypeDecl._unwrap(value.range_type)
        
        low_bound = Expr._unwrap(value.low_bound)
        
        high_bound = Expr._unwrap(value.high_bound)

        result = cls._Holder(cls._c_type(
            
            range_type=range_type,
            
            low_bound=low_bound,
            
            high_bound=high_bound,
        ))


        return result







class DiscriminantValues(_BaseStruct):
    """
    Represent a set of values (as a list of choices) on a discriminant.
    """

    

    __slots__ = ('_discriminant', '_values')

    def __init__(
        self,
        discriminant: Identifier,
        values: AlternativesList,
    ):
        self._discriminant = discriminant
        self._values = values


    @property
    def discriminant(self) -> Identifier:
        """

        """
        return self._discriminant

    @property
    def values(self) -> AlternativesList:
        """

        """
        return self._values

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('discriminant',
            _Entity_c_type
         ),
        ('values',
            _Entity_c_type
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            AdaNode._wrap(c_value.discriminant),
            AdaNode._wrap(c_value.values),
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        discriminant = Identifier._unwrap(value.discriminant)
        
        values = AlternativesList._unwrap(value.values)

        result = cls._Holder(cls._c_type(
            
            discriminant=discriminant,
            
            values=values,
        ))


        return result







class DocAnnotation(_BaseStruct):
    """
    Documentation annotation.
    """

    

    __slots__ = ('_key', '_value')

    def __init__(
        self,
        key: str,
        value: str,
    ):
        self._key = key
        self._value = value


    @property
    def key(self) -> str:
        """
        Annotation key
        """
        return self._key

    @property
    def value(self) -> str:
        """
        Annotation value
        """
        return self._value

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('key',
            _String.c_type
         ),
        ('value',
            _String.c_type
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            if self.c_value is not None:
                DocAnnotation._dec_ref(self.c_value)
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            _String.wrap(c_value.key),
            _String.wrap(c_value.value),
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        key = _String.unwrap(value.key)
        
        value = _String.unwrap(value.value)

        result = cls._Holder(cls._c_type(
            
            key=key.c_value,
            
            value=value.c_value,
        ))

        cls._inc_ref(result.c_value)

        return result

    _c_ptr_type = ctypes.POINTER(_c_type)
    _inc_ref = staticmethod(_import_func('ada_internal_doc_annotation_inc_ref',
                            [_c_ptr_type], None))
    _dec_ref = staticmethod(_import_func('ada_internal_doc_annotation_dec_ref',
                            [_c_ptr_type], None))






class LogicContext(_BaseStruct):
    """
    Describes an interpretation of a reference. Can be attached to logic atoms
    (e.g. Binds) to indicate under which interpretation this particular atom
    was produced, which can in turn be used to produce informative diagnostics
    for resolution failures.
    """

    

    __slots__ = ('_ref_node', '_decl_node')

    def __init__(
        self,
        ref_node: AdaNode,
        decl_node: AdaNode,
    ):
        self._ref_node = ref_node
        self._decl_node = decl_node


    @property
    def ref_node(self) -> AdaNode:
        """

        """
        return self._ref_node

    @property
    def decl_node(self) -> AdaNode:
        """

        """
        return self._decl_node

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('ref_node',
            _Entity_c_type
         ),
        ('decl_node',
            _Entity_c_type
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            AdaNode._wrap(c_value.ref_node),
            AdaNode._wrap(c_value.decl_node),
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        ref_node = AdaNode._unwrap(value.ref_node)
        
        decl_node = AdaNode._unwrap(value.decl_node)

        result = cls._Holder(cls._c_type(
            
            ref_node=ref_node,
            
            decl_node=decl_node,
        ))


        return result







class ParamActual(_BaseStruct):
    """
    Data structure used by zip_with_params, Name.call_params,
    GenericInstantiation.inst_params, BaseAggregate.aggregate_params,
    SubtypeIndication.subtype_constraints, and EnumRepClause.params properties.
    Associates an expression (the actual) to a formal param declaration (the
    parameter).
    """

    

    __slots__ = ('_param', '_actual')

    def __init__(
        self,
        param: DefiningName,
        actual: Expr,
    ):
        self._param = param
        self._actual = actual


    @property
    def param(self) -> DefiningName:
        """

        """
        return self._param

    @property
    def actual(self) -> Expr:
        """

        """
        return self._actual

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('param',
            _Entity_c_type
         ),
        ('actual',
            _Entity_c_type
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            AdaNode._wrap(c_value.param),
            AdaNode._wrap(c_value.actual),
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        param = DefiningName._unwrap(value.param)
        
        actual = Expr._unwrap(value.actual)

        result = cls._Holder(cls._c_type(
            
            param=param,
            
            actual=actual,
        ))


        return result







class RefResult(_BaseStruct):
    """
    Result for a cross reference query returning a reference.
    """

    

    __slots__ = ('_ref', '_kind')

    def __init__(
        self,
        ref: BaseId,
        kind: str,
    ):
        self._ref = ref
        self._kind = kind


    @property
    def ref(self) -> BaseId:
        """

        """
        return self._ref

    @property
    def kind(self) -> str:
        """

        """
        return self._kind

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('ref',
            _Entity_c_type
         ),
        ('kind',
            ctypes.c_int
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            AdaNode._wrap(c_value.ref),
            RefResultKind._wrap(c_value.kind),
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        ref = BaseId._unwrap(value.ref)
        
        kind = RefResultKind._unwrap(value.kind)

        result = cls._Holder(cls._c_type(
            
            ref=ref,
            
            kind=kind,
        ))


        return result







class RefdDecl(_BaseStruct):
    """
    Result for a cross reference query returning a referenced decl.
    """

    

    __slots__ = ('_decl', '_def_name', '_kind')

    def __init__(
        self,
        decl: BasicDecl,
        def_name: DefiningName,
        kind: str,
    ):
        self._decl = decl
        self._def_name = def_name
        self._kind = kind


    @property
    def decl(self) -> BasicDecl:
        """

        """
        return self._decl

    @property
    def def_name(self) -> DefiningName:
        """

        """
        return self._def_name

    @property
    def kind(self) -> str:
        """

        """
        return self._kind

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('decl',
            _Entity_c_type
         ),
        ('def_name',
            _Entity_c_type
         ),
        ('kind',
            ctypes.c_int
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            AdaNode._wrap(c_value.decl),
            AdaNode._wrap(c_value.def_name),
            RefResultKind._wrap(c_value.kind),
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        decl = BasicDecl._unwrap(value.decl)
        
        def_name = DefiningName._unwrap(value.def_name)
        
        kind = RefResultKind._unwrap(value.kind)

        result = cls._Holder(cls._c_type(
            
            decl=decl,
            
            def_name=def_name,
            
            kind=kind,
        ))


        return result







class Shape(_BaseStruct):
    """
    Represent one of the shapes that a variant record can have, as a list of
    the available components.
    """

    

    __slots__ = ('_components', '_discriminants_values')

    def __init__(
        self,
        components: List[BaseFormalParamDecl],
        discriminants_values: List[DiscriminantValues],
    ):
        self._components = components
        self._discriminants_values = discriminants_values


    @property
    def components(self) -> List[BaseFormalParamDecl]:
        """

        """
        return self._components

    @property
    def discriminants_values(self) -> List[DiscriminantValues]:
        """

        """
        return self._discriminants_values

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('components',
             ctypes.c_void_p
         ),
        ('discriminants_values',
             ctypes.c_void_p
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            if self.c_value is not None:
                Shape._dec_ref(self.c_value)
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            _AdaNodeArrayConverter.wrap(ctypes.cast(c_value.components, _AdaNodeArrayConverter.c_type), True),
            _DiscriminantValuesArrayConverter.wrap(ctypes.cast(c_value.discriminants_values, _DiscriminantValuesArrayConverter.c_type), True),
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        components = _AdaNodeArrayConverter.unwrap(value.components)
        
        discriminants_values = _DiscriminantValuesArrayConverter.unwrap(value.discriminants_values)

        result = cls._Holder(cls._c_type(
            
            components=ctypes.cast(components.c_value, ctypes.c_void_p),
            
            discriminants_values=ctypes.cast(discriminants_values.c_value, ctypes.c_void_p),
        ))

        cls._inc_ref(result.c_value)

        return result

    _c_ptr_type = ctypes.POINTER(_c_type)
    _inc_ref = staticmethod(_import_func('ada_internal_shape_inc_ref',
                            [_c_ptr_type], None))
    _dec_ref = staticmethod(_import_func('ada_internal_shape_dec_ref',
                            [_c_ptr_type], None))






class SolverDiagnostic(_BaseStruct):
    """
    A raw diagnostic produced by a solver resolution failure. This contains as
    much information as possible to allow formatters down the chain to
    filter/choose which diagnostics to show among a set of diagnostics produced
    for a single equation.

    * ``Message_Template`` is a string explaining the error, which may contain
      holes represented by the ``{}`` characters. Literal opening braces are
      encoded as ``{{``.

    * ``Args`` is an array of nodes, which are to be plugged in the holes of
      the template in the same order (i.e. the first argument goes into the
      first hole of the template, etc.).

    * ``Location`` is a node which indicates the location of the error.

    * ``Contexts`` is the array of contexts that were deemed relevant for this
      error.

    * ``Round`` is the solver round during which this diagnostic was emitted.
    """

    

    __slots__ = ('_message_template', '_args', '_location', '_contexts', '_round')

    def __init__(
        self,
        message_template: str,
        args: List[AdaNode],
        location: AdaNode,
        contexts: List[LogicContext],
        round: int,
    ):
        self._message_template = message_template
        self._args = args
        self._location = location
        self._contexts = contexts
        self._round = round


    @property
    def message_template(self) -> str:
        """

        """
        return self._message_template

    @property
    def args(self) -> List[AdaNode]:
        """

        """
        return self._args

    @property
    def location(self) -> AdaNode:
        """

        """
        return self._location

    @property
    def contexts(self) -> List[LogicContext]:
        """

        """
        return self._contexts

    @property
    def round(self) -> int:
        """

        """
        return self._round

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('message_template',
            _String.c_type
         ),
        ('args',
             ctypes.c_void_p
         ),
        ('location',
            AdaNode._node_c_type
         ),
        ('contexts',
             ctypes.c_void_p
         ),
        ('round',
            ctypes.c_int
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            if self.c_value is not None:
                SolverDiagnostic._dec_ref(self.c_value)
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            _String.wrap(c_value.message_template),
            _AdaNodeArrayConverter.wrap(ctypes.cast(c_value.args, _AdaNodeArrayConverter.c_type), True),
            AdaNode._wrap_bare_node(c_value.location),
            _LogicContextArrayConverter.wrap(ctypes.cast(c_value.contexts, _LogicContextArrayConverter.c_type), True),
            c_value.round,
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        message_template = _String.unwrap(value.message_template)
        
        args = _AdaNodeArrayConverter.unwrap(value.args)
        
        location = value.location._node_c_value
        
        contexts = _LogicContextArrayConverter.unwrap(value.contexts)
        
        round = int(value.round)

        result = cls._Holder(cls._c_type(
            
            message_template=message_template.c_value,
            
            args=ctypes.cast(args.c_value, ctypes.c_void_p),
            
            location=location,
            
            contexts=ctypes.cast(contexts.c_value, ctypes.c_void_p),
            
            round=round,
        ))

        cls._inc_ref(result.c_value)

        return result

    _c_ptr_type = ctypes.POINTER(_c_type)
    _inc_ref = staticmethod(_import_func('ada_internal_solver_diagnostic_inc_ref',
                            [_c_ptr_type], None))
    _dec_ref = staticmethod(_import_func('ada_internal_solver_diagnostic_dec_ref',
                            [_c_ptr_type], None))






class Substitution(_BaseStruct):
    """
    Represent a substitution of a BasicDecl by a given value. This can then be
    used as part of an environment in the eval_as_*_in_env property. See the
    declaration of those properties for more details.
    """

    

    __slots__ = ('_from_decl', '_to_value', '_value_type')

    def __init__(
        self,
        from_decl: BasicDecl,
        to_value: int,
        value_type: BaseTypeDecl,
    ):
        self._from_decl = from_decl
        self._to_value = to_value
        self._value_type = value_type


    @property
    def from_decl(self) -> BasicDecl:
        """
        The declaration to substitute.
        """
        return self._from_decl

    @property
    def to_value(self) -> int:
        """
        The value by which to substitute the declaration.
        """
        return self._to_value

    @property
    def value_type(self) -> BaseTypeDecl:
        """
        The type of the substituted value.
        """
        return self._value_type

    class _c_type(ctypes.Structure):
        _fields_ =  [
        ('from_decl',
            _Entity_c_type
         ),
        ('to_value',
            _big_integer.c_type
         ),
        ('value_type',
            _Entity_c_type
         ),
] 

    class _Holder:
        def __init__(self, c_value):
            self.c_value = c_value

        def clear(self):
            self.c_value = None

        def __del__(self):
            if self.c_value is not None:
                Substitution._dec_ref(self.c_value)
            self.clear()

    @classmethod
    def _wrap(cls, c_value):
        return cls(
            AdaNode._wrap(c_value.from_decl),
            _big_integer.wrap(c_value.to_value),
            AdaNode._wrap(c_value.value_type),
        )

    @classmethod
    def _unwrap(cls, value, context=None):
        if not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)

        
        from_decl = BasicDecl._unwrap(value.from_decl)
        
        to_value = _big_integer.unwrap(value.to_value)
        
        value_type = BaseTypeDecl._unwrap(value.value_type)

        result = cls._Holder(cls._c_type(
            
            from_decl=from_decl,
            
            to_value=to_value.c_value,
            
            value_type=value_type,
        ))

        cls._inc_ref(result.c_value)

        return result

    _c_ptr_type = ctypes.POINTER(_c_type)
    _inc_ref = staticmethod(_import_func('ada_internal_substitution_inc_ref',
                            [_c_ptr_type], None))
    _dec_ref = staticmethod(_import_func('ada_internal_substitution_dec_ref',
                            [_c_ptr_type], None))




_Metadata_c_type._null_value = _Metadata_c_type(False, False, False)
_EntityInfo_c_type._null_value = _EntityInfo_c_type(_Metadata_c_type._null_value,
                                                None)


#
# Low-level binding - Second part
#

# For performance, allocate a single C API entity for all uses of null
# entities.
_Entity_c_type._null_value = _Entity_c_type()
_Entity_c_type._null_value.node = None



class _BaseArray:
    """
    Base class for Ada arrays bindings.
    """

    c_element_type: ClassVar[Any]
    """
    Ctype class for array elements.
    """

    items_refcounted = False
    """
    Whether items for this arrays are ref-counted.
    """

    __slots__ = ('c_value', 'length', 'items')

    def __init__(self, c_value):
        self.c_value = c_value

        self.length = c_value.contents.n

        items_addr = _field_address(c_value.contents, 'items')
        items = self.c_element_type.from_address(items_addr)
        self.items = ctypes.pointer(items)

    def __repr__(self):
        return '<{} {}>'.format(type(self).__name__, list(self))

    def clear(self):
        self.c_value = None
        self.length = None
        self.items = None

    def __del__(self):
        self.dec_ref(self.c_value)
        self.clear()

    @classmethod
    def wrap(cls, c_value, from_field_access):
        helper = cls(c_value)

        result = []
        for i in range(helper.length):
            # In ctypes, accessing an array element does not copy it, which
            # means the the array must live at least as long as the accessed
            # element. We cannot guarantee that, so we must copy the element so
            # that it is independent of the array it comes from.
            #
            # The try/except block tries to do a copy if "item" is indeed a
            # buffer to be copied, and will fail if it's a mere integer, which
            # does not need the buffer copy anyway, hence the "pass".
            item = helper.items[i]
            try:
                item = cls.c_element_type.from_buffer_copy(item)
            except TypeError:
                pass
            result.append(helper.wrap_item(item))

        # If this array value comes from a structure field, we must not call
        # its dec_ref primitive, as it is up to the structure's dec_ref
        # primitive to take care of it.
        if from_field_access:
            helper.clear()

        return result

    @classmethod
    def unwrap(cls, value, context=None):
        if not isinstance(value, list):
            _raise_type_error('list', value)

        # Create a holder for the result
        result = cls(cls.create(len(value)))

        # Unwrap all items at once, preserving their holder so that resources
        # are deallocated if there is an error at some point.
        items = [result.unwrap_item(item, context) for item in value]

        # Initialize the resulting array
        for i, (_, item) in enumerate(items):
            result.items[i] = item

        # At this point, we know that this is successful. We don't want
        # holders to dec-ref the content so that the return array takes over
        # the corresponding refcounting shares.
        if cls.items_refcounted:
            for holder, _ in items:
                holder.clear()

        return result






class _AspectArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalAspect.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = False

    @staticmethod
    def wrap_item(item):
        return Aspect._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = Aspect._unwrap(item)
        c_value = c_holder.c_value
        return (c_holder, c_value)

    c_element_type = Aspect._c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', Aspect._c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_internal_aspect_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_internal_aspect_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_internal_aspect_array_dec_ref', [c_type], None))






class _DiscriminantValuesArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalDiscriminantValues.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = False

    @staticmethod
    def wrap_item(item):
        return DiscriminantValues._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = DiscriminantValues._unwrap(item)
        c_value = c_holder.c_value
        return (c_holder, c_value)

    c_element_type = DiscriminantValues._c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', DiscriminantValues._c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_internal_discriminant_values_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_internal_discriminant_values_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_internal_discriminant_values_array_dec_ref', [c_type], None))






class _DocAnnotationArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalDocAnnotation.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = True

    @staticmethod
    def wrap_item(item):
        return DocAnnotation._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = DocAnnotation._unwrap(item)
        c_value = c_holder.c_value
        return (c_holder, c_value)

    c_element_type = DocAnnotation._c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', DocAnnotation._c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_internal_doc_annotation_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_internal_doc_annotation_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_internal_doc_annotation_array_dec_ref', [c_type], None))






class _AdaNodeArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalEntity.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = False

    @staticmethod
    def wrap_item(item):
        return AdaNode._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = AdaNode._unwrap(item)
        c_value = c_holder
        return (c_holder, c_value)

    c_element_type = _Entity_c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', _Entity_c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_node_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_node_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_node_array_dec_ref', [c_type], None))






class _LogicContextArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalLogicContext.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = False

    @staticmethod
    def wrap_item(item):
        return LogicContext._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = LogicContext._unwrap(item)
        c_value = c_holder.c_value
        return (c_holder, c_value)

    c_element_type = LogicContext._c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', LogicContext._c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_internal_logic_context_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_internal_logic_context_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_internal_logic_context_array_dec_ref', [c_type], None))






class _ParamActualArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalParamActual.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = False

    @staticmethod
    def wrap_item(item):
        return ParamActual._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = ParamActual._unwrap(item)
        c_value = c_holder.c_value
        return (c_holder, c_value)

    c_element_type = ParamActual._c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', ParamActual._c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_internal_param_actual_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_internal_param_actual_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_internal_param_actual_array_dec_ref', [c_type], None))






class _RefResultArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalRefResult.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = False

    @staticmethod
    def wrap_item(item):
        return RefResult._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = RefResult._unwrap(item)
        c_value = c_holder.c_value
        return (c_holder, c_value)

    c_element_type = RefResult._c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', RefResult._c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_internal_ref_result_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_internal_ref_result_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_internal_ref_result_array_dec_ref', [c_type], None))






class _ShapeArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalShape.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = True

    @staticmethod
    def wrap_item(item):
        return Shape._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = Shape._unwrap(item)
        c_value = c_holder.c_value
        return (c_holder, c_value)

    c_element_type = Shape._c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', Shape._c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_internal_shape_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_internal_shape_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_internal_shape_array_dec_ref', [c_type], None))






class _SolverDiagnosticArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalSolverDiagnostic.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = True

    @staticmethod
    def wrap_item(item):
        return SolverDiagnostic._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = SolverDiagnostic._unwrap(item)
        c_value = c_holder.c_value
        return (c_holder, c_value)

    c_element_type = SolverDiagnostic._c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', SolverDiagnostic._c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_internal_solver_diagnostic_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_internal_solver_diagnostic_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_internal_solver_diagnostic_array_dec_ref', [c_type], None))






class _SubstitutionArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalSubstitution.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = True

    @staticmethod
    def wrap_item(item):
        return Substitution._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = Substitution._unwrap(item)
        c_value = c_holder.c_value
        return (c_holder, c_value)

    c_element_type = Substitution._c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', Substitution._c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_internal_substitution_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_internal_substitution_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_internal_substitution_array_dec_ref', [c_type], None))






class _AnalysisUnitArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of InternalUnit.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = False

    @staticmethod
    def wrap_item(item):
        return AnalysisUnit._wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = AnalysisUnit._unwrap(item)
        c_value = c_holder
        return (c_holder, c_value)

    c_element_type = AnalysisUnit._c_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', AnalysisUnit._c_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_analysis_unit_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_analysis_unit_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_analysis_unit_array_dec_ref', [c_type], None))






class _UnboundedTextTypeArrayConverter(_BaseArray):
    """
    Wrapper class for arrays of SymbolType.

    This class is not meant to be directly instantiated: it is only used to
    convert values that various methods take/return.
    """

    __slots__ = _BaseArray.__slots__
    items_refcounted = False

    @staticmethod
    def wrap_item(item):
        return _symbol_type.wrap(item)

    @staticmethod
    def unwrap_item(item, context=None):
        c_holder = _symbol_type.unwrap(item, context)
        c_value = c_holder
        return (c_holder, c_value)

    c_element_type = _symbol_type

    class c_struct(ctypes.Structure):
        _fields_ = [('n', ctypes.c_int),
                    ('ref_count', ctypes.c_int),
                    ('items', _symbol_type * 1)]

    c_type = ctypes.POINTER(c_struct)

    create = staticmethod(_import_func(
        'ada_symbol_type_array_create', [ctypes.c_int], c_type))
    inc_ref = staticmethod(_import_func(
        'ada_symbol_type_array_inc_ref', [c_type], None))
    dec_ref = staticmethod(_import_func(
        'ada_symbol_type_array_dec_ref', [c_type], None))





_IteratedType = TypeVar("_IteratedType")

class _BaseIterator(Generic[_IteratedType]):
    """
Base class for Ada iterator bindings.

An iterator provides a mean to retrieve values one-at-a-time.

Currently, each iterator is bound to the analysis context used to create it.
Iterators are invalidated as soon as any unit of that analysis is reparsed. Due
to the nature of iterators (lazy computations), this invalidation is necessary
to avoid use of inconsistent state, such as an iterator trying to use analysis
context data that is stale.
"""

    _c_element_type: ClassVar[Any]
    """
    Ctype class for iterator elements.
    """

    __slots__ = ('_c_value',)

    def __init__(self, c_value: Any):
        self._c_value = c_value

    def __repr__(self) -> str:
        return '<{}>'.format(type(self).__name__)

    def _clear(self) -> None:
        self._c_value = None

    def __del__(self) -> None:
        self._dec_ref(self._c_value)
        self._clear()

    @classmethod
    def _wrap(cls, c_value: Any) -> Opt[_BaseIterator]:
        return cls(c_value) if c_value else None

    @classmethod
    def unwrap(cls, value: Opt[_BaseIterator]) -> Any:
        if value is None:
            return None
        elif not isinstance(value, cls):
            _raise_type_error(cls.__name__, value)
        else:
            return value._c_value

    def __iter__(self) -> Iterator[_IteratedType]:
        return self

    def __next__(self) -> _IteratedType:
        """
      Return the next value from the iterator. Raises ``StopIteration`` if
      there is no more element to retrieve.

      This raises a ``Stale_Reference_Error`` exception if the iterator is
      invalidated.
      """
        x = self._c_element_type()
        if self._get_next(self._c_value, ctypes.byref(x)):
            return self._wrap_item(x)
        raise StopIteration

    # For Python2 compatibility
    next = __next__

    # The following methods are just for type hints: subclasses override them

    @staticmethod
    def _get_next(c_value: Any, item_ptr: Any) -> Any:
        pass

    @staticmethod
    def _inc_ref(c_value: Any) -> None:
        pass

    @staticmethod
    def _dec_ref(c_value: Any) -> None:
        pass

    @staticmethod
    def _wrap_item(item: Any) -> _IteratedType:
        pass






class CompletionItemIterator(_BaseIterator[CompletionItem]):
    """
    Iterator over InternalCompletionItem.

    This class is not meant to be directly instantiated: it is only used for
    for the return values of properties returning iterators.
    """

    __slots__ = _BaseIterator.__slots__

    @staticmethod
    def _wrap_item(item):
        return CompletionItem._wrap(item)

    _c_element_type = CompletionItem._c_type
    _c_element_type_ptr = ctypes.POINTER(_c_element_type)

    _c_type = ctypes.c_void_p

    _get_next = staticmethod(_import_func(
        'ada_internal_completion_item_iterator_next',
        [_c_type, _c_element_type_ptr],
        ctypes.c_int))
    _inc_ref = staticmethod(_import_func(
        'ada_internal_completion_item_iterator_inc_ref', [_c_type], None))
    _dec_ref = staticmethod(_import_func(
        'ada_internal_completion_item_iterator_dec_ref', [_c_type], None))




_free = _import_func(
    'ada_free',
    [ctypes.c_void_p], None
)

_destroy_text = _import_func(
    'ada_destroy_text', [ctypes.POINTER(_text)], None
)

_symbol_text = _import_func(
    'ada_symbol_text',
    [ctypes.POINTER(_symbol_type), ctypes.POINTER(_text)], None
)

_get_versions = _import_func(
    'ada_get_versions',
    [ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p)], None
)

# Analysis primitives
_allocate_analysis_context = _import_func(
    'ada_allocate_analysis_context',
    [],
    AnalysisContext._c_type,
)
_initialize_analysis_context = _import_func(
    'ada_initialize_analysis_context',
    [AnalysisContext._c_type, # context
     ctypes.c_char_p,         # charset
     _file_reader,            # file_reader
     _unit_provider,          # unit_provider
     _event_handler,          # event_handler
     ctypes.c_int,            # with_trivia
     ctypes.c_int],           # tab_stop
    None,
)
_context_incref = _import_func(
    'ada_context_incref',
    [AnalysisContext._c_type], AnalysisContext._c_type
)
_context_decref = _import_func(
    'ada_context_decref',
    [AnalysisContext._c_type], None
)
_context_symbol = _import_func(
    'ada_context_symbol',
    [AnalysisContext._c_type,
     ctypes.POINTER(_text),
     ctypes.POINTER(_symbol_type)], ctypes.c_int
)
_discard_errors_in_populate_lexical_env = _import_func(
   'ada_context_discard_errors_in_populate_lexical_env',
   [AnalysisContext._c_type, ctypes.c_int], None
)
_get_analysis_unit_from_file = _import_func(
    'ada_get_analysis_unit_from_file',
    [AnalysisContext._c_type,  # context
     ctypes.c_char_p,          # filename
     ctypes.c_char_p,          # charset
     ctypes.c_int,             # reparse
     ctypes.c_int],            # grammar rule
    AnalysisUnit._c_type
)
_get_analysis_unit_from_buffer = _import_func(
    'ada_get_analysis_unit_from_buffer',
    [AnalysisContext._c_type,  # context
     ctypes.c_char_p,          # filename
     ctypes.c_char_p,          # charset
     ctypes.c_char_p,          # buffer
     ctypes.c_size_t,          # buffer_size
     ctypes.c_int],            # grammar rule
    AnalysisUnit._c_type
)
_get_analysis_unit_from_provider = _import_func(
    'ada_get_analysis_unit_from_provider',
    [AnalysisContext._c_type,  # context
     ctypes.POINTER(_text),    # name
     ctypes.c_int,             # kind
     ctypes.c_char_p,          # charset
     ctypes.c_int],            # reparse
    AnalysisUnit._c_type
)
_unit_root = _import_func(
    'ada_unit_root',
    [AnalysisUnit._c_type, ctypes.POINTER(_Entity_c_type)], None
)
_unit_first_token = _import_func(
    "ada_unit_first_token",
    [AnalysisUnit._c_type, Token._c_type], None
)
_unit_last_token = _import_func(
    "ada_unit_last_token",
    [AnalysisUnit._c_type, Token._c_type], None
)
_unit_token_count = _import_func(
    "ada_unit_token_count",
    [AnalysisUnit._c_type], ctypes.c_int
)
_unit_trivia_count = _import_func(
    "ada_unit_trivia_count",
    [AnalysisUnit._c_type], ctypes.c_int
)
_unit_lookup_token = _import_func(
    "ada_unit_lookup_token",
    [AnalysisUnit._c_type,
     ctypes.POINTER(Sloc._c_type),
     Token._c_type],
    None
)
_unit_dump_lexical_env = _import_func(
    "ada_unit_dump_lexical_env",
    [AnalysisUnit._c_type], None
)
_unit_filename = _import_func(
    "ada_unit_filename",
    [AnalysisUnit._c_type], ctypes.POINTER(ctypes.c_char)
)
_unit_diagnostic_count = _import_func(
    'ada_unit_diagnostic_count',
    [AnalysisUnit._c_type], ctypes.c_uint
)
_unit_diagnostic = _import_func(
    'ada_unit_diagnostic',
    [AnalysisUnit._c_type, ctypes.c_uint, ctypes.POINTER(Diagnostic._c_type)],
    ctypes.c_int
)
_unit_context = _import_func(
    'ada_unit_context',
    [AnalysisUnit._c_type], AnalysisContext._c_type
)
_unit_reparse_from_file = _import_func(
    'ada_unit_reparse_from_file',
    [AnalysisUnit._c_type,    # unit
     ctypes.c_char_p],        # charset
    ctypes.c_int
)
_unit_reparse_from_buffer = _import_func(
    'ada_unit_reparse_from_buffer',
    [AnalysisUnit._c_type, # unit
     ctypes.c_char_p,      # charset
     ctypes.c_char_p,      # buffer
     ctypes.c_size_t],     # buffer_size
    None
)
_unit_populate_lexical_env = _import_func(
    'ada_unit_populate_lexical_env',
    [
        AnalysisUnit._c_type,
            ctypes.c_int,
    ],
    ctypes.c_int
)

# General AST node primitives
_node_hash = _import_func(
    'ada_node_hash',
    [ctypes.POINTER(_Entity_c_type)], ctypes.c_uint32
)

_node_is_equivalent = _import_func(
    'ada_node_is_equivalent',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)], ctypes.c_uint8
)

_node_kind = _import_func(
    'ada_node_kind',
    [ctypes.POINTER(_Entity_c_type)], ctypes.c_int
)
_node_unit = _import_func(
    'ada_node_unit',
    [ctypes.POINTER(_Entity_c_type)], AnalysisUnit._c_type
)
_node_is_token_node = _import_func(
    'ada_node_is_token_node',
    [ctypes.POINTER(_Entity_c_type)], ctypes.c_int
)
_node_is_synthetic = _import_func(
    'ada_node_is_synthetic',
    [ctypes.POINTER(_Entity_c_type)], ctypes.c_int
)
_node_image = _import_func(
    'ada_node_image',
    [ctypes.POINTER(_Entity_c_type), ctypes.POINTER(_text)], None
)
_node_text = _import_func(
    'ada_node_text',
    [ctypes.POINTER(_Entity_c_type), ctypes.POINTER(_text)], None
)
_node_sloc_range = _import_func(
    'ada_node_sloc_range',
    [ctypes.POINTER(_Entity_c_type), ctypes.POINTER(SlocRange._c_type)], None
)
_lookup_in_node = _import_func(
    'ada_lookup_in_node',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(Sloc._c_type),
     ctypes.POINTER(_Entity_c_type)], None
)
_node_children_count = _import_func(
    'ada_node_children_count',
    [ctypes.POINTER(_Entity_c_type)], ctypes.c_uint
)
_node_child = _import_func(
    'ada_node_child',
    [ctypes.POINTER(_Entity_c_type), ctypes.c_uint, ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)

_ada_node_parent = _import_func(
    'ada_ada_node_parent',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_parents = _import_func(
    'ada_ada_node_parents',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_ada_node_children = _import_func(
    'ada_ada_node_children',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_ada_node_token_start = _import_func(
    'ada_ada_node_token_start',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(Token._c_struct)],
    ctypes.c_int
)
_ada_node_token_end = _import_func(
    'ada_ada_node_token_end',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(Token._c_struct)],
    ctypes.c_int
)
_ada_node_child_index = _import_func(
    'ada_ada_node_child_index',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_int)],
    ctypes.c_int
)
_ada_node_previous_sibling = _import_func(
    'ada_ada_node_previous_sibling',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_next_sibling = _import_func(
    'ada_ada_node_next_sibling',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_unit = _import_func(
    'ada_ada_node_unit',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(AnalysisUnit._c_type)],
    ctypes.c_int
)
_ada_node_is_ghost = _import_func(
    'ada_ada_node_is_ghost',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_ada_node_full_sloc_image = _import_func(
    'ada_ada_node_full_sloc_image',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_ada_node_completion_item_kind_to_int = _import_func(
    'ada_ada_node_completion_item_kind_to_int',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_int,
     ctypes.POINTER(ctypes.c_int)],
    ctypes.c_int
)
_ada_node_p_declarative_scope = _import_func(
    'ada_ada_node_p_declarative_scope',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_enclosing_compilation_unit = _import_func(
    'ada_ada_node_p_enclosing_compilation_unit',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_get_uninstantiated_node = _import_func(
    'ada_ada_node_p_get_uninstantiated_node',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_complete = _import_func(
    'ada_ada_node_p_complete',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(CompletionItemIterator._c_type)],
    ctypes.c_int
)
_ada_node_p_valid_keywords = _import_func(
    'ada_ada_node_p_valid_keywords',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_UnboundedTextTypeArrayConverter.c_type)],
    ctypes.c_int
)
_ada_node_p_generic_instantiations = _import_func(
    'ada_ada_node_p_generic_instantiations',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_ada_node_p_semantic_parent = _import_func(
    'ada_ada_node_p_semantic_parent',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_parent_basic_decl = _import_func(
    'ada_ada_node_p_parent_basic_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_has_spark_mode_on = _import_func(
    'ada_ada_node_p_has_spark_mode_on',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_ada_node_p_is_subject_to_proof = _import_func(
    'ada_ada_node_p_is_subject_to_proof',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_ada_node_p_filter_is_imported_by = _import_func(
    'ada_ada_node_p_filter_is_imported_by',
    [ctypes.POINTER(_Entity_c_type),
        
        _AnalysisUnitArrayConverter.c_type,
        
        ctypes.c_uint8,
     ctypes.POINTER(_AnalysisUnitArrayConverter.c_type)],
    ctypes.c_int
)
_ada_node_p_resolve_names = _import_func(
    'ada_ada_node_p_resolve_names',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_ada_node_p_nameres_diagnostics = _import_func(
    'ada_ada_node_p_nameres_diagnostics',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_SolverDiagnosticArrayConverter.c_type)],
    ctypes.c_int
)
_ada_node_p_standard_unit = _import_func(
    'ada_ada_node_p_standard_unit',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(AnalysisUnit._c_type)],
    ctypes.c_int
)
_ada_node_p_is_keyword = _import_func(
    'ada_ada_node_p_is_keyword',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(Token._c_struct),
        
        ctypes.POINTER(_symbol_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_ada_node_p_std_entity = _import_func(
    'ada_ada_node_p_std_entity',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_bool_type = _import_func(
    'ada_ada_node_p_bool_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_int_type = _import_func(
    'ada_ada_node_p_int_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_universal_int_type = _import_func(
    'ada_ada_node_p_universal_int_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_universal_real_type = _import_func(
    'ada_ada_node_p_universal_real_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_std_char_type = _import_func(
    'ada_ada_node_p_std_char_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_std_wide_char_type = _import_func(
    'ada_ada_node_p_std_wide_char_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_std_wide_wide_char_type = _import_func(
    'ada_ada_node_p_std_wide_wide_char_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_std_string_type = _import_func(
    'ada_ada_node_p_std_string_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_std_wide_string_type = _import_func(
    'ada_ada_node_p_std_wide_string_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_std_wide_wide_string_type = _import_func(
    'ada_ada_node_p_std_wide_wide_string_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_top_level_decl = _import_func(
    'ada_ada_node_p_top_level_decl',
    [ctypes.POINTER(_Entity_c_type),
        
        AnalysisUnit._c_type,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_choice_match = _import_func(
    'ada_ada_node_p_choice_match',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_big_integer.c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_ada_node_p_gnat_xref = _import_func(
    'ada_ada_node_p_gnat_xref',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ada_node_p_spark_mode_aspect = _import_func(
    'ada_ada_node_p_spark_mode_aspect',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(Aspect._c_type)],
    ctypes.c_int
)
_ada_node_p_xref_entry_point = _import_func(
    'ada_ada_node_p_xref_entry_point',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_ada_node_p_matching_with_use_clause = _import_func(
    'ada_ada_node_p_matching_with_use_clause',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_abort_node_p_as_bool = _import_func(
    'ada_abort_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_abstract_node_p_as_bool = _import_func(
    'ada_abstract_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_assoc_list_p_zip_with_params = _import_func(
    'ada_assoc_list_p_zip_with_params',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_ParamActualArrayConverter.c_type)],
    ctypes.c_int
)
_aliased_node_p_as_bool = _import_func(
    'ada_aliased_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_all_node_p_as_bool = _import_func(
    'ada_all_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_constrained_array_indices_f_list = _import_func(
    'ada_constrained_array_indices_f_list',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_unconstrained_array_indices_f_types = _import_func(
    'ada_unconstrained_array_indices_f_types',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_aspect_assoc_f_id = _import_func(
    'ada_aspect_assoc_f_id',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_aspect_assoc_f_expr = _import_func(
    'ada_aspect_assoc_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_aspect_assoc_p_is_ghost_code = _import_func(
    'ada_aspect_assoc_p_is_ghost_code',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_at_clause_f_name = _import_func(
    'ada_at_clause_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_at_clause_f_expr = _import_func(
    'ada_at_clause_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_attribute_def_clause_f_attribute_expr = _import_func(
    'ada_attribute_def_clause_f_attribute_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_attribute_def_clause_f_expr = _import_func(
    'ada_attribute_def_clause_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_enum_rep_clause_f_type_name = _import_func(
    'ada_enum_rep_clause_f_type_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_enum_rep_clause_f_aggregate = _import_func(
    'ada_enum_rep_clause_f_aggregate',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_enum_rep_clause_p_params = _import_func(
    'ada_enum_rep_clause_p_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_ParamActualArrayConverter.c_type)],
    ctypes.c_int
)
_record_rep_clause_f_name = _import_func(
    'ada_record_rep_clause_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_record_rep_clause_f_at_expr = _import_func(
    'ada_record_rep_clause_f_at_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_record_rep_clause_f_components = _import_func(
    'ada_record_rep_clause_f_components',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_aspect_spec_f_aspect_assocs = _import_func(
    'ada_aspect_spec_f_aspect_assocs',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_assoc_p_assoc_expr = _import_func(
    'ada_base_assoc_p_assoc_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_contract_case_assoc_f_guard = _import_func(
    'ada_contract_case_assoc_f_guard',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_contract_case_assoc_f_consequence = _import_func(
    'ada_contract_case_assoc_f_consequence',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_pragma_argument_assoc_f_name = _import_func(
    'ada_pragma_argument_assoc_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_pragma_argument_assoc_f_expr = _import_func(
    'ada_pragma_argument_assoc_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_formal_param_holder_p_abstract_formal_params = _import_func(
    'ada_base_formal_param_holder_p_abstract_formal_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_base_formal_param_holder_p_formal_params = _import_func(
    'ada_base_formal_param_holder_p_formal_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_base_formal_param_holder_p_nb_min_params = _import_func(
    'ada_base_formal_param_holder_p_nb_min_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_int)],
    ctypes.c_int
)
_base_formal_param_holder_p_nb_max_params = _import_func(
    'ada_base_formal_param_holder_p_nb_max_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_int)],
    ctypes.c_int
)
_base_formal_param_holder_p_param_types = _import_func(
    'ada_base_formal_param_holder_p_param_types',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_base_subp_spec_p_name = _import_func(
    'ada_base_subp_spec_p_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_subp_spec_p_returns = _import_func(
    'ada_base_subp_spec_p_returns',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_subp_spec_p_primitive_subp_types = _import_func(
    'ada_base_subp_spec_p_primitive_subp_types',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_base_subp_spec_p_primitive_subp_first_type = _import_func(
    'ada_base_subp_spec_p_primitive_subp_first_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_subp_spec_p_primitive_subp_tagged_type = _import_func(
    'ada_base_subp_spec_p_primitive_subp_tagged_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_subp_spec_p_return_type = _import_func(
    'ada_base_subp_spec_p_return_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_subp_spec_p_params = _import_func(
    'ada_base_subp_spec_p_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_entry_spec_f_entry_name = _import_func(
    'ada_entry_spec_f_entry_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_spec_f_family_type = _import_func(
    'ada_entry_spec_f_family_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_spec_f_entry_params = _import_func(
    'ada_entry_spec_f_entry_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subp_spec_f_subp_kind = _import_func(
    'ada_subp_spec_f_subp_kind',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subp_spec_f_subp_name = _import_func(
    'ada_subp_spec_f_subp_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subp_spec_f_subp_params = _import_func(
    'ada_subp_spec_f_subp_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subp_spec_f_subp_returns = _import_func(
    'ada_subp_spec_f_subp_returns',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_synthetic_binary_spec_f_left_param = _import_func(
    'ada_synthetic_binary_spec_f_left_param',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_synthetic_binary_spec_f_right_param = _import_func(
    'ada_synthetic_binary_spec_f_right_param',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_synthetic_binary_spec_f_return_type_expr = _import_func(
    'ada_synthetic_binary_spec_f_return_type_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_synthetic_unary_spec_f_right_param = _import_func(
    'ada_synthetic_unary_spec_f_right_param',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_synthetic_unary_spec_f_return_type_expr = _import_func(
    'ada_synthetic_unary_spec_f_return_type_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_component_list_f_components = _import_func(
    'ada_component_list_f_components',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_component_list_f_variant_part = _import_func(
    'ada_component_list_f_variant_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_known_discriminant_part_f_discr_specs = _import_func(
    'ada_known_discriminant_part_f_discr_specs',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_completion_formal_params_f_params = _import_func(
    'ada_entry_completion_formal_params_f_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_formal_part_f_decls = _import_func(
    'ada_generic_formal_part_f_decls',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_record_def_f_components = _import_func(
    'ada_base_record_def_f_components',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_assoc_p_get_params = _import_func(
    'ada_basic_assoc_p_get_params',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_aggregate_assoc_f_designators = _import_func(
    'ada_aggregate_assoc_f_designators',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_aggregate_assoc_f_r_expr = _import_func(
    'ada_aggregate_assoc_f_r_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_composite_constraint_assoc_f_ids = _import_func(
    'ada_composite_constraint_assoc_f_ids',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_composite_constraint_assoc_f_constraint_expr = _import_func(
    'ada_composite_constraint_assoc_f_constraint_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_iterated_assoc_f_spec = _import_func(
    'ada_iterated_assoc_f_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_iterated_assoc_f_key_expr = _import_func(
    'ada_iterated_assoc_f_key_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_iterated_assoc_f_r_expr = _import_func(
    'ada_iterated_assoc_f_r_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_param_assoc_f_designator = _import_func(
    'ada_param_assoc_f_designator',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_param_assoc_f_r_expr = _import_func(
    'ada_param_assoc_f_r_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_f_aspects = _import_func(
    'ada_basic_decl_f_aspects',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_is_formal = _import_func(
    'ada_basic_decl_p_is_formal',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_basic_decl_p_doc_annotations = _import_func(
    'ada_basic_decl_p_doc_annotations',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_DocAnnotationArrayConverter.c_type)],
    ctypes.c_int
)
_basic_decl_p_doc = _import_func(
    'ada_basic_decl_p_doc',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_basic_decl_p_canonical_part = _import_func(
    'ada_basic_decl_p_canonical_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_all_parts = _import_func(
    'ada_basic_decl_p_all_parts',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_basic_decl_p_get_aspect_assoc = _import_func(
    'ada_basic_decl_p_get_aspect_assoc',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_get_aspect_spec_expr = _import_func(
    'ada_basic_decl_p_get_aspect_spec_expr',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_get_aspect = _import_func(
    'ada_basic_decl_p_get_aspect',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
        
        ctypes.c_uint8,
        
        ctypes.c_uint8,
     ctypes.POINTER(Aspect._c_type)],
    ctypes.c_int
)
_basic_decl_p_has_aspect = _import_func(
    'ada_basic_decl_p_has_aspect',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
        
        ctypes.c_uint8,
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_basic_decl_p_get_pragma = _import_func(
    'ada_basic_decl_p_get_pragma',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_get_representation_clause = _import_func(
    'ada_basic_decl_p_get_representation_clause',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_get_at_clause = _import_func(
    'ada_basic_decl_p_get_at_clause',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_get_annotations = _import_func(
    'ada_basic_decl_p_get_annotations',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AspectArrayConverter.c_type)],
    ctypes.c_int
)
_basic_decl_p_is_ghost_code = _import_func(
    'ada_basic_decl_p_is_ghost_code',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_basic_decl_p_is_compilation_unit_root = _import_func(
    'ada_basic_decl_p_is_compilation_unit_root',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_basic_decl_p_is_visible = _import_func(
    'ada_basic_decl_p_is_visible',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_basic_decl_p_base_subp_declarations = _import_func(
    'ada_basic_decl_p_base_subp_declarations',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_basic_decl_p_root_subp_declarations = _import_func(
    'ada_basic_decl_p_root_subp_declarations',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_basic_decl_p_find_all_overrides = _import_func(
    'ada_basic_decl_p_find_all_overrides',
    [ctypes.POINTER(_Entity_c_type),
        
        _AnalysisUnitArrayConverter.c_type,
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_basic_decl_p_defining_names = _import_func(
    'ada_basic_decl_p_defining_names',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_basic_decl_p_defining_name = _import_func(
    'ada_basic_decl_p_defining_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_subp_spec_or_null = _import_func(
    'ada_basic_decl_p_subp_spec_or_null',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_is_subprogram = _import_func(
    'ada_basic_decl_p_is_subprogram',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_basic_decl_p_is_predefined_operator = _import_func(
    'ada_basic_decl_p_is_predefined_operator',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_basic_decl_p_relative_name = _import_func(
    'ada_basic_decl_p_relative_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_relative_name_text = _import_func(
    'ada_basic_decl_p_relative_name_text',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_symbol_type)],
    ctypes.c_int
)
_basic_decl_p_body_part_for_decl = _import_func(
    'ada_basic_decl_p_body_part_for_decl',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_most_visible_part = _import_func(
    'ada_basic_decl_p_most_visible_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_fully_qualified_name_array = _import_func(
    'ada_basic_decl_p_fully_qualified_name_array',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_UnboundedTextTypeArrayConverter.c_type)],
    ctypes.c_int
)
_basic_decl_p_fully_qualified_name = _import_func(
    'ada_basic_decl_p_fully_qualified_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_basic_decl_p_canonical_fully_qualified_name = _import_func(
    'ada_basic_decl_p_canonical_fully_qualified_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_basic_decl_p_unique_identifying_name = _import_func(
    'ada_basic_decl_p_unique_identifying_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_basic_decl_p_previous_part_for_decl = _import_func(
    'ada_basic_decl_p_previous_part_for_decl',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_is_static_decl = _import_func(
    'ada_basic_decl_p_is_static_decl',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_basic_decl_p_is_imported = _import_func(
    'ada_basic_decl_p_is_imported',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_basic_decl_p_type_expression = _import_func(
    'ada_basic_decl_p_type_expression',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_next_part_for_decl = _import_func(
    'ada_basic_decl_p_next_part_for_decl',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_decl_p_is_constant_object = _import_func(
    'ada_basic_decl_p_is_constant_object',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_abstract_state_decl_f_name = _import_func(
    'ada_abstract_state_decl_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_anonymous_expr_decl_f_name = _import_func(
    'ada_anonymous_expr_decl_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_anonymous_expr_decl_f_expr = _import_func(
    'ada_anonymous_expr_decl_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_anonymous_expr_decl_p_get_formal = _import_func(
    'ada_anonymous_expr_decl_p_get_formal',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_formal_param_decl_p_formal_type = _import_func(
    'ada_base_formal_param_decl_p_formal_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_component_decl_f_ids = _import_func(
    'ada_component_decl_f_ids',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_component_decl_f_component_def = _import_func(
    'ada_component_decl_f_component_def',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_component_decl_f_default_expr = _import_func(
    'ada_component_decl_f_default_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_discriminant_spec_f_ids = _import_func(
    'ada_discriminant_spec_f_ids',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_discriminant_spec_f_type_expr = _import_func(
    'ada_discriminant_spec_f_type_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_discriminant_spec_f_default_expr = _import_func(
    'ada_discriminant_spec_f_default_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_formal_f_decl = _import_func(
    'ada_generic_formal_f_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_param_spec_f_ids = _import_func(
    'ada_param_spec_f_ids',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_param_spec_f_has_aliased = _import_func(
    'ada_param_spec_f_has_aliased',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_param_spec_f_mode = _import_func(
    'ada_param_spec_f_mode',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_param_spec_f_type_expr = _import_func(
    'ada_param_spec_f_type_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_param_spec_f_default_expr = _import_func(
    'ada_param_spec_f_default_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_synthetic_formal_param_decl_f_param_type = _import_func(
    'ada_synthetic_formal_param_decl_f_param_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_package_decl_f_package_name = _import_func(
    'ada_base_package_decl_f_package_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_package_decl_f_public_part = _import_func(
    'ada_base_package_decl_f_public_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_package_decl_f_private_part = _import_func(
    'ada_base_package_decl_f_private_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_package_decl_f_end_name = _import_func(
    'ada_base_package_decl_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_package_decl_p_body_part = _import_func(
    'ada_base_package_decl_p_body_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_f_name = _import_func(
    'ada_base_type_decl_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_base_subtype = _import_func(
    'ada_base_type_decl_p_base_subtype',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_attribute_subprogram = _import_func(
    'ada_base_type_decl_p_attribute_subprogram',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_private_completion = _import_func(
    'ada_base_type_decl_p_private_completion',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_is_inherited_primitive = _import_func(
    'ada_base_type_decl_p_is_inherited_primitive',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_get_record_representation_clause = _import_func(
    'ada_base_type_decl_p_get_record_representation_clause',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_get_enum_representation_clause = _import_func(
    'ada_base_type_decl_p_get_enum_representation_clause',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_get_primitives = _import_func(
    'ada_base_type_decl_p_get_primitives',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_base_type_decl_p_is_array_type = _import_func(
    'ada_base_type_decl_p_is_array_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_find_derived_types = _import_func(
    'ada_base_type_decl_p_find_derived_types',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_base_type_decl_p_is_scalar_type = _import_func(
    'ada_base_type_decl_p_is_scalar_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_specific_type = _import_func(
    'ada_base_type_decl_p_specific_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_find_all_derived_types = _import_func(
    'ada_base_type_decl_p_find_all_derived_types',
    [ctypes.POINTER(_Entity_c_type),
        
        _AnalysisUnitArrayConverter.c_type,
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_base_type_decl_p_comp_type = _import_func(
    'ada_base_type_decl_p_comp_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_index_type = _import_func(
    'ada_base_type_decl_p_index_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_int,
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_is_derived_type = _import_func(
    'ada_base_type_decl_p_is_derived_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_is_limited_type = _import_func(
    'ada_base_type_decl_p_is_limited_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_matching_type = _import_func(
    'ada_base_type_decl_p_matching_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_previous_part = _import_func(
    'ada_base_type_decl_p_previous_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_next_part = _import_func(
    'ada_base_type_decl_p_next_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_full_view = _import_func(
    'ada_base_type_decl_p_full_view',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_is_definite_subtype = _import_func(
    'ada_base_type_decl_p_is_definite_subtype',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_is_statically_constrained = _import_func(
    'ada_base_type_decl_p_is_statically_constrained',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_discriminants_list = _import_func(
    'ada_base_type_decl_p_discriminants_list',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_base_type_decl_p_shapes = _import_func(
    'ada_base_type_decl_p_shapes',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_ShapeArrayConverter.c_type)],
    ctypes.c_int
)
_base_type_decl_p_is_record_type = _import_func(
    'ada_base_type_decl_p_is_record_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_is_real_type = _import_func(
    'ada_base_type_decl_p_is_real_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_is_float_type = _import_func(
    'ada_base_type_decl_p_is_float_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_is_fixed_point = _import_func(
    'ada_base_type_decl_p_is_fixed_point',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_is_enum_type = _import_func(
    'ada_base_type_decl_p_is_enum_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_is_access_type = _import_func(
    'ada_base_type_decl_p_is_access_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_is_char_type = _import_func(
    'ada_base_type_decl_p_is_char_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_classwide_type = _import_func(
    'ada_base_type_decl_p_classwide_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_discrete_range = _import_func(
    'ada_base_type_decl_p_discrete_range',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(DiscreteRange._c_type)],
    ctypes.c_int
)
_base_type_decl_p_is_discrete_type = _import_func(
    'ada_base_type_decl_p_is_discrete_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_is_int_type = _import_func(
    'ada_base_type_decl_p_is_int_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_accessed_type = _import_func(
    'ada_base_type_decl_p_accessed_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_is_tagged_type = _import_func(
    'ada_base_type_decl_p_is_tagged_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_is_abstract_type = _import_func(
    'ada_base_type_decl_p_is_abstract_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_base_type = _import_func(
    'ada_base_type_decl_p_base_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_base_types = _import_func(
    'ada_base_type_decl_p_base_types',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_base_type_decl_p_is_interface_type = _import_func(
    'ada_base_type_decl_p_is_interface_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_canonical_type = _import_func(
    'ada_base_type_decl_p_canonical_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_type_decl_p_is_private = _import_func(
    'ada_base_type_decl_p_is_private',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_type_decl_p_root_type = _import_func(
    'ada_base_type_decl_p_root_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_subtype_decl_p_get_type = _import_func(
    'ada_base_subtype_decl_p_get_type',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subtype_decl_f_subtype = _import_func(
    'ada_subtype_decl_f_subtype',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_incomplete_type_decl_f_discriminants = _import_func(
    'ada_incomplete_type_decl_f_discriminants',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_incomplete_formal_type_decl_f_is_tagged = _import_func(
    'ada_incomplete_formal_type_decl_f_is_tagged',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_incomplete_formal_type_decl_f_default_type = _import_func(
    'ada_incomplete_formal_type_decl_f_default_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_incomplete_tagged_type_decl_f_has_abstract = _import_func(
    'ada_incomplete_tagged_type_decl_f_has_abstract',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_protected_type_decl_f_discriminants = _import_func(
    'ada_protected_type_decl_f_discriminants',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_protected_type_decl_f_interfaces = _import_func(
    'ada_protected_type_decl_f_interfaces',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_protected_type_decl_f_definition = _import_func(
    'ada_protected_type_decl_f_definition',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_task_type_decl_f_discriminants = _import_func(
    'ada_task_type_decl_f_discriminants',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_task_type_decl_f_definition = _import_func(
    'ada_task_type_decl_f_definition',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_type_decl_f_discriminants = _import_func(
    'ada_type_decl_f_discriminants',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_type_decl_f_type_def = _import_func(
    'ada_type_decl_f_type_def',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_formal_type_decl_f_default_type = _import_func(
    'ada_formal_type_decl_f_default_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_basic_subp_decl_p_subp_decl_spec = _import_func(
    'ada_basic_subp_decl_p_subp_decl_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_classic_subp_decl_f_overriding = _import_func(
    'ada_classic_subp_decl_f_overriding',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_classic_subp_decl_f_subp_spec = _import_func(
    'ada_classic_subp_decl_f_subp_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_classic_subp_decl_p_body_part = _import_func(
    'ada_classic_subp_decl_p_body_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_formal_subp_decl_f_default_expr = _import_func(
    'ada_formal_subp_decl_f_default_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_decl_f_overriding = _import_func(
    'ada_entry_decl_f_overriding',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_decl_f_spec = _import_func(
    'ada_entry_decl_f_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_decl_p_body_part = _import_func(
    'ada_entry_decl_p_body_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_decl_p_accept_stmts = _import_func(
    'ada_entry_decl_p_accept_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_enum_literal_decl_f_name = _import_func(
    'ada_enum_literal_decl_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_enum_literal_decl_p_enum_rep = _import_func(
    'ada_enum_literal_decl_p_enum_rep',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_big_integer.c_type)],
    ctypes.c_int
)
_enum_literal_decl_p_enum_type = _import_func(
    'ada_enum_literal_decl_p_enum_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_synthetic_char_enum_lit_p_expr = _import_func(
    'ada_synthetic_char_enum_lit_p_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_internal_f_subp_spec = _import_func(
    'ada_generic_subp_internal_f_subp_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_synthetic_subp_decl_f_spec = _import_func(
    'ada_synthetic_subp_decl_f_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_body_node_p_previous_part = _import_func(
    'ada_body_node_p_previous_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_body_node_p_decl_part = _import_func(
    'ada_body_node_p_decl_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_body_node_p_subunit_root = _import_func(
    'ada_body_node_p_subunit_root',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_accept_stmt_body_f_name = _import_func(
    'ada_accept_stmt_body_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_subp_body_f_overriding = _import_func(
    'ada_base_subp_body_f_overriding',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_subp_body_f_subp_spec = _import_func(
    'ada_base_subp_body_f_subp_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_expr_function_f_expr = _import_func(
    'ada_expr_function_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subp_body_f_decls = _import_func(
    'ada_subp_body_f_decls',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subp_body_f_stmts = _import_func(
    'ada_subp_body_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subp_body_f_end_name = _import_func(
    'ada_subp_body_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subp_renaming_decl_f_renames = _import_func(
    'ada_subp_renaming_decl_f_renames',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_body_stub_p_syntactic_fully_qualified_name = _import_func(
    'ada_body_stub_p_syntactic_fully_qualified_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_UnboundedTextTypeArrayConverter.c_type)],
    ctypes.c_int
)
_package_body_stub_f_name = _import_func(
    'ada_package_body_stub_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_protected_body_stub_f_name = _import_func(
    'ada_protected_body_stub_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subp_body_stub_f_overriding = _import_func(
    'ada_subp_body_stub_f_overriding',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subp_body_stub_f_subp_spec = _import_func(
    'ada_subp_body_stub_f_subp_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_task_body_stub_f_name = _import_func(
    'ada_task_body_stub_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_body_f_entry_name = _import_func(
    'ada_entry_body_f_entry_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_body_f_index_spec = _import_func(
    'ada_entry_body_f_index_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_body_f_params = _import_func(
    'ada_entry_body_f_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_body_f_barrier = _import_func(
    'ada_entry_body_f_barrier',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_body_f_decls = _import_func(
    'ada_entry_body_f_decls',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_body_f_stmts = _import_func(
    'ada_entry_body_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_body_f_end_name = _import_func(
    'ada_entry_body_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_package_body_f_package_name = _import_func(
    'ada_package_body_f_package_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_package_body_f_decls = _import_func(
    'ada_package_body_f_decls',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_package_body_f_stmts = _import_func(
    'ada_package_body_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_package_body_f_end_name = _import_func(
    'ada_package_body_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_protected_body_f_name = _import_func(
    'ada_protected_body_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_protected_body_f_decls = _import_func(
    'ada_protected_body_f_decls',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_protected_body_f_end_name = _import_func(
    'ada_protected_body_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_task_body_f_name = _import_func(
    'ada_task_body_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_task_body_f_decls = _import_func(
    'ada_task_body_f_decls',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_task_body_f_stmts = _import_func(
    'ada_task_body_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_task_body_f_end_name = _import_func(
    'ada_task_body_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_index_spec_f_id = _import_func(
    'ada_entry_index_spec_f_id',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_entry_index_spec_f_subtype = _import_func(
    'ada_entry_index_spec_f_subtype',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_exception_decl_f_ids = _import_func(
    'ada_exception_decl_f_ids',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_exception_decl_f_renames = _import_func(
    'ada_exception_decl_f_renames',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_exception_handler_f_exception_name = _import_func(
    'ada_exception_handler_f_exception_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_exception_handler_f_handled_exceptions = _import_func(
    'ada_exception_handler_f_handled_exceptions',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_exception_handler_f_stmts = _import_func(
    'ada_exception_handler_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_for_loop_var_decl_f_id = _import_func(
    'ada_for_loop_var_decl_f_id',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_for_loop_var_decl_f_id_type = _import_func(
    'ada_for_loop_var_decl_f_id_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_decl_f_formal_part = _import_func(
    'ada_generic_decl_f_formal_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_package_decl_f_package_decl = _import_func(
    'ada_generic_package_decl_f_package_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_package_decl_p_body_part = _import_func(
    'ada_generic_package_decl_p_body_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_decl_f_subp_decl = _import_func(
    'ada_generic_subp_decl_f_subp_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_decl_p_body_part = _import_func(
    'ada_generic_subp_decl_p_body_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_instantiation_p_designated_generic_decl = _import_func(
    'ada_generic_instantiation_p_designated_generic_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_instantiation_p_inst_params = _import_func(
    'ada_generic_instantiation_p_inst_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_ParamActualArrayConverter.c_type)],
    ctypes.c_int
)
_generic_package_instantiation_f_name = _import_func(
    'ada_generic_package_instantiation_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_package_instantiation_f_generic_pkg_name = _import_func(
    'ada_generic_package_instantiation_f_generic_pkg_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_package_instantiation_f_params = _import_func(
    'ada_generic_package_instantiation_f_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_instantiation_f_overriding = _import_func(
    'ada_generic_subp_instantiation_f_overriding',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_instantiation_f_kind = _import_func(
    'ada_generic_subp_instantiation_f_kind',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_instantiation_f_subp_name = _import_func(
    'ada_generic_subp_instantiation_f_subp_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_instantiation_f_generic_subp_name = _import_func(
    'ada_generic_subp_instantiation_f_generic_subp_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_instantiation_f_params = _import_func(
    'ada_generic_subp_instantiation_f_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_package_renaming_decl_f_name = _import_func(
    'ada_generic_package_renaming_decl_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_package_renaming_decl_f_renames = _import_func(
    'ada_generic_package_renaming_decl_f_renames',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_renaming_decl_f_kind = _import_func(
    'ada_generic_subp_renaming_decl_f_kind',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_renaming_decl_f_name = _import_func(
    'ada_generic_subp_renaming_decl_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_generic_subp_renaming_decl_f_renames = _import_func(
    'ada_generic_subp_renaming_decl_f_renames',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_label_decl_f_name = _import_func(
    'ada_label_decl_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_named_stmt_decl_f_name = _import_func(
    'ada_named_stmt_decl_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_number_decl_f_ids = _import_func(
    'ada_number_decl_f_ids',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_number_decl_f_expr = _import_func(
    'ada_number_decl_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_object_decl_f_ids = _import_func(
    'ada_object_decl_f_ids',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_object_decl_f_has_aliased = _import_func(
    'ada_object_decl_f_has_aliased',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_object_decl_f_has_constant = _import_func(
    'ada_object_decl_f_has_constant',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_object_decl_f_mode = _import_func(
    'ada_object_decl_f_mode',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_object_decl_f_type_expr = _import_func(
    'ada_object_decl_f_type_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_object_decl_f_default_expr = _import_func(
    'ada_object_decl_f_default_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_object_decl_f_renaming_clause = _import_func(
    'ada_object_decl_f_renaming_clause',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_object_decl_p_is_statically_constrained = _import_func(
    'ada_object_decl_p_is_statically_constrained',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_object_decl_p_private_part_decl = _import_func(
    'ada_object_decl_p_private_part_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_object_decl_p_public_part_decl = _import_func(
    'ada_object_decl_p_public_part_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_package_renaming_decl_f_name = _import_func(
    'ada_package_renaming_decl_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_package_renaming_decl_f_renames = _import_func(
    'ada_package_renaming_decl_f_renames',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_package_renaming_decl_p_renamed_package = _import_func(
    'ada_package_renaming_decl_p_renamed_package',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_package_renaming_decl_p_final_renamed_package = _import_func(
    'ada_package_renaming_decl_p_final_renamed_package',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_single_protected_decl_f_name = _import_func(
    'ada_single_protected_decl_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_single_protected_decl_f_interfaces = _import_func(
    'ada_single_protected_decl_f_interfaces',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_single_protected_decl_f_definition = _import_func(
    'ada_single_protected_decl_f_definition',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_single_task_decl_f_task_type = _import_func(
    'ada_single_task_decl_f_task_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_case_stmt_alternative_f_choices = _import_func(
    'ada_case_stmt_alternative_f_choices',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_case_stmt_alternative_f_stmts = _import_func(
    'ada_case_stmt_alternative_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_compilation_unit_f_prelude = _import_func(
    'ada_compilation_unit_f_prelude',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_compilation_unit_f_body = _import_func(
    'ada_compilation_unit_f_body',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_compilation_unit_f_pragmas = _import_func(
    'ada_compilation_unit_f_pragmas',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_compilation_unit_p_syntactic_fully_qualified_name = _import_func(
    'ada_compilation_unit_p_syntactic_fully_qualified_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_UnboundedTextTypeArrayConverter.c_type)],
    ctypes.c_int
)
_compilation_unit_p_unit_kind = _import_func(
    'ada_compilation_unit_p_unit_kind',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_int)],
    ctypes.c_int
)
_compilation_unit_p_withed_units = _import_func(
    'ada_compilation_unit_p_withed_units',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_compilation_unit_p_imported_units = _import_func(
    'ada_compilation_unit_p_imported_units',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_compilation_unit_p_unit_dependencies = _import_func(
    'ada_compilation_unit_p_unit_dependencies',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_compilation_unit_p_decl = _import_func(
    'ada_compilation_unit_p_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_compilation_unit_p_is_preelaborable = _import_func(
    'ada_compilation_unit_p_is_preelaborable',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_compilation_unit_p_other_part = _import_func(
    'ada_compilation_unit_p_other_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_compilation_unit_p_has_restriction = _import_func(
    'ada_compilation_unit_p_has_restriction',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_compilation_unit_p_all_config_pragmas = _import_func(
    'ada_compilation_unit_p_all_config_pragmas',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_compilation_unit_p_config_pragmas = _import_func(
    'ada_compilation_unit_p_config_pragmas',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_component_clause_f_id = _import_func(
    'ada_component_clause_f_id',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_component_clause_f_position = _import_func(
    'ada_component_clause_f_position',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_component_clause_f_range = _import_func(
    'ada_component_clause_f_range',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_component_def_f_has_aliased = _import_func(
    'ada_component_def_f_has_aliased',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_component_def_f_has_constant = _import_func(
    'ada_component_def_f_has_constant',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_component_def_f_type_expr = _import_func(
    'ada_component_def_f_type_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_constant_node_p_as_bool = _import_func(
    'ada_constant_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_composite_constraint_f_constraints = _import_func(
    'ada_composite_constraint_f_constraints',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_composite_constraint_p_is_index_constraint = _import_func(
    'ada_composite_constraint_p_is_index_constraint',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_composite_constraint_p_is_discriminant_constraint = _import_func(
    'ada_composite_constraint_p_is_discriminant_constraint',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_composite_constraint_p_discriminant_params = _import_func(
    'ada_composite_constraint_p_discriminant_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_ParamActualArrayConverter.c_type)],
    ctypes.c_int
)
_delta_constraint_f_delta = _import_func(
    'ada_delta_constraint_f_delta',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_delta_constraint_f_range = _import_func(
    'ada_delta_constraint_f_range',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_digits_constraint_f_digits = _import_func(
    'ada_digits_constraint_f_digits',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_digits_constraint_f_range = _import_func(
    'ada_digits_constraint_f_range',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_range_constraint_f_range = _import_func(
    'ada_range_constraint_f_range',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_declarative_part_f_decls = _import_func(
    'ada_declarative_part_f_decls',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_else_part_f_stmts = _import_func(
    'ada_else_part_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_elsif_expr_part_f_cond_expr = _import_func(
    'ada_elsif_expr_part_f_cond_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_elsif_expr_part_f_then_expr = _import_func(
    'ada_elsif_expr_part_f_then_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_elsif_stmt_part_f_cond_expr = _import_func(
    'ada_elsif_stmt_part_f_cond_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_elsif_stmt_part_f_stmts = _import_func(
    'ada_elsif_stmt_part_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_expr_p_expression_type = _import_func(
    'ada_expr_p_expression_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_expr_p_expected_expression_type = _import_func(
    'ada_expr_p_expected_expression_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_expr_p_is_dynamically_tagged = _import_func(
    'ada_expr_p_is_dynamically_tagged',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_expr_p_is_static_expr = _import_func(
    'ada_expr_p_is_static_expr',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_expr_p_eval_as_int = _import_func(
    'ada_expr_p_eval_as_int',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_big_integer.c_type)],
    ctypes.c_int
)
_expr_p_eval_as_int_in_env = _import_func(
    'ada_expr_p_eval_as_int_in_env',
    [ctypes.POINTER(_Entity_c_type),
        
        _SubstitutionArrayConverter.c_type,
     ctypes.POINTER(_big_integer.c_type)],
    ctypes.c_int
)
_expr_p_eval_as_string = _import_func(
    'ada_expr_p_eval_as_string',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_expr_p_eval_as_string_in_env = _import_func(
    'ada_expr_p_eval_as_string_in_env',
    [ctypes.POINTER(_Entity_c_type),
        
        _SubstitutionArrayConverter.c_type,
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_expr_p_matching_nodes = _import_func(
    'ada_expr_p_matching_nodes',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_expr_p_is_dispatching_call = _import_func(
    'ada_expr_p_is_dispatching_call',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_expr_p_first_corresponding_decl = _import_func(
    'ada_expr_p_first_corresponding_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_abstract_state_decl_expr_f_state_decl = _import_func(
    'ada_abstract_state_decl_expr_f_state_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_allocator_f_subpool = _import_func(
    'ada_allocator_f_subpool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_allocator_f_type_or_expr = _import_func(
    'ada_allocator_f_type_or_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_allocator_p_get_allocated_type = _import_func(
    'ada_allocator_p_get_allocated_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_aggregate_f_ancestor_expr = _import_func(
    'ada_base_aggregate_f_ancestor_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_aggregate_f_assocs = _import_func(
    'ada_base_aggregate_f_assocs',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_aggregate_p_aggregate_params = _import_func(
    'ada_base_aggregate_p_aggregate_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_ParamActualArrayConverter.c_type)],
    ctypes.c_int
)
_base_aggregate_p_is_subaggregate = _import_func(
    'ada_base_aggregate_p_is_subaggregate',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_base_aggregate_p_subaggregate_array_type = _import_func(
    'ada_base_aggregate_p_subaggregate_array_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_aggregate_p_subaggregate_dimension = _import_func(
    'ada_base_aggregate_p_subaggregate_dimension',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_int)],
    ctypes.c_int
)
_bin_op_f_left = _import_func(
    'ada_bin_op_f_left',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_bin_op_f_op = _import_func(
    'ada_bin_op_f_op',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_bin_op_f_right = _import_func(
    'ada_bin_op_f_right',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_case_expr_alternative_f_choices = _import_func(
    'ada_case_expr_alternative_f_choices',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_case_expr_alternative_f_expr = _import_func(
    'ada_case_expr_alternative_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_concat_op_f_first_operand = _import_func(
    'ada_concat_op_f_first_operand',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_concat_op_f_other_operands = _import_func(
    'ada_concat_op_f_other_operands',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_concat_op_p_operands = _import_func(
    'ada_concat_op_p_operands',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_concat_operand_f_operator = _import_func(
    'ada_concat_operand_f_operator',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_concat_operand_f_operand = _import_func(
    'ada_concat_operand_f_operand',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_cond_expr_p_dependent_exprs = _import_func(
    'ada_cond_expr_p_dependent_exprs',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_case_expr_f_expr = _import_func(
    'ada_case_expr_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_case_expr_f_cases = _import_func(
    'ada_case_expr_f_cases',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_if_expr_f_cond_expr = _import_func(
    'ada_if_expr_f_cond_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_if_expr_f_then_expr = _import_func(
    'ada_if_expr_f_then_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_if_expr_f_alternatives = _import_func(
    'ada_if_expr_f_alternatives',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_if_expr_f_else_expr = _import_func(
    'ada_if_expr_f_else_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_contract_cases_f_contract_cases = _import_func(
    'ada_contract_cases_f_contract_cases',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_decl_expr_f_decls = _import_func(
    'ada_decl_expr_f_decls',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_decl_expr_f_expr = _import_func(
    'ada_decl_expr_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_format_string_literal_f_opening_chunk = _import_func(
    'ada_format_string_literal_f_opening_chunk',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_format_string_literal_f_mid_exprs = _import_func(
    'ada_format_string_literal_f_mid_exprs',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_format_string_literal_f_trailing_expr = _import_func(
    'ada_format_string_literal_f_trailing_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_membership_expr_f_expr = _import_func(
    'ada_membership_expr_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_membership_expr_f_op = _import_func(
    'ada_membership_expr_f_op',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_membership_expr_f_membership_exprs = _import_func(
    'ada_membership_expr_f_membership_exprs',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_name_p_enclosing_defining_name = _import_func(
    'ada_name_p_enclosing_defining_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_name_p_is_defining = _import_func(
    'ada_name_p_is_defining',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_name_is = _import_func(
    'ada_name_p_name_is',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_is_direct_call = _import_func(
    'ada_name_p_is_direct_call',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_is_access_call = _import_func(
    'ada_name_p_is_access_call',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_is_call = _import_func(
    'ada_name_p_is_call',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_is_dot_call = _import_func(
    'ada_name_p_is_dot_call',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_all_env_elements = _import_func(
    'ada_name_p_all_env_elements',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_name_p_called_subp_spec = _import_func(
    'ada_name_p_called_subp_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_name_p_referenced_decl = _import_func(
    'ada_name_p_referenced_decl',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_name_p_referenced_defining_name = _import_func(
    'ada_name_p_referenced_defining_name',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_name_p_failsafe_referenced_decl = _import_func(
    'ada_name_p_failsafe_referenced_decl',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(RefdDecl._c_type)],
    ctypes.c_int
)
_name_p_name_designated_type = _import_func(
    'ada_name_p_name_designated_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_name_p_is_static_subtype = _import_func(
    'ada_name_p_is_static_subtype',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_name_matches = _import_func(
    'ada_name_p_name_matches',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_is_operator_name = _import_func(
    'ada_name_p_is_operator_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_is_write_reference = _import_func(
    'ada_name_p_is_write_reference',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_is_static_call = _import_func(
    'ada_name_p_is_static_call',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_name_p_call_params = _import_func(
    'ada_name_p_call_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_ParamActualArrayConverter.c_type)],
    ctypes.c_int
)
_name_p_relative_name = _import_func(
    'ada_name_p_relative_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_name_p_as_symbol_array = _import_func(
    'ada_name_p_as_symbol_array',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_UnboundedTextTypeArrayConverter.c_type)],
    ctypes.c_int
)
_name_p_canonical_text = _import_func(
    'ada_name_p_canonical_text',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_symbol_type)],
    ctypes.c_int
)
_name_p_is_constant = _import_func(
    'ada_name_p_is_constant',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_array_subcomponent_choice_name_f_name = _import_func(
    'ada_array_subcomponent_choice_name_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_array_subcomponent_choice_name_f_suffix = _import_func(
    'ada_array_subcomponent_choice_name_f_suffix',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_attribute_ref_f_prefix = _import_func(
    'ada_attribute_ref_f_prefix',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_attribute_ref_f_attribute = _import_func(
    'ada_attribute_ref_f_attribute',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_attribute_ref_f_args = _import_func(
    'ada_attribute_ref_f_args',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_call_expr_f_name = _import_func(
    'ada_call_expr_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_call_expr_f_suffix = _import_func(
    'ada_call_expr_f_suffix',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_call_expr_p_kind = _import_func(
    'ada_call_expr_p_kind',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_int)],
    ctypes.c_int
)
_call_expr_p_is_array_slice = _import_func(
    'ada_call_expr_p_is_array_slice',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_defining_name_f_name = _import_func(
    'ada_defining_name_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_defining_name_p_canonical_fully_qualified_name = _import_func(
    'ada_defining_name_p_canonical_fully_qualified_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_defining_name_p_unique_identifying_name = _import_func(
    'ada_defining_name_p_unique_identifying_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_defining_name_p_fully_qualified_name_array = _import_func(
    'ada_defining_name_p_fully_qualified_name_array',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_UnboundedTextTypeArrayConverter.c_type)],
    ctypes.c_int
)
_defining_name_p_fully_qualified_name = _import_func(
    'ada_defining_name_p_fully_qualified_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_defining_name_p_basic_decl = _import_func(
    'ada_defining_name_p_basic_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_defining_name_p_find_refs = _import_func(
    'ada_defining_name_p_find_refs',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_RefResultArrayConverter.c_type)],
    ctypes.c_int
)
_defining_name_p_find_all_references = _import_func(
    'ada_defining_name_p_find_all_references',
    [ctypes.POINTER(_Entity_c_type),
        
        _AnalysisUnitArrayConverter.c_type,
        
        ctypes.c_uint8,
        
        ctypes.c_uint8,
     ctypes.POINTER(_RefResultArrayConverter.c_type)],
    ctypes.c_int
)
_defining_name_p_find_all_calls = _import_func(
    'ada_defining_name_p_find_all_calls',
    [ctypes.POINTER(_Entity_c_type),
        
        _AnalysisUnitArrayConverter.c_type,
        
        ctypes.c_uint8,
        
        ctypes.c_uint8,
     ctypes.POINTER(_RefResultArrayConverter.c_type)],
    ctypes.c_int
)
_defining_name_p_next_part = _import_func(
    'ada_defining_name_p_next_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_defining_name_p_previous_part = _import_func(
    'ada_defining_name_p_previous_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_defining_name_p_canonical_part = _import_func(
    'ada_defining_name_p_canonical_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_defining_name_p_most_visible_part = _import_func(
    'ada_defining_name_p_most_visible_part',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_defining_name_p_all_parts = _import_func(
    'ada_defining_name_p_all_parts',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_defining_name_p_get_aspect = _import_func(
    'ada_defining_name_p_get_aspect',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
        
        ctypes.c_uint8,
        
        ctypes.c_uint8,
     ctypes.POINTER(Aspect._c_type)],
    ctypes.c_int
)
_defining_name_p_has_aspect = _import_func(
    'ada_defining_name_p_has_aspect',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
        
        ctypes.c_uint8,
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_defining_name_p_get_pragma = _import_func(
    'ada_defining_name_p_get_pragma',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_defining_name_p_get_representation_clause = _import_func(
    'ada_defining_name_p_get_representation_clause',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_symbol_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_defining_name_p_get_at_clause = _import_func(
    'ada_defining_name_p_get_at_clause',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_defining_name_p_get_annotations = _import_func(
    'ada_defining_name_p_get_annotations',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(_AspectArrayConverter.c_type)],
    ctypes.c_int
)
_defining_name_p_is_imported = _import_func(
    'ada_defining_name_p_is_imported',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_defining_name_p_is_ghost_code = _import_func(
    'ada_defining_name_p_is_ghost_code',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_discrete_subtype_name_f_subtype = _import_func(
    'ada_discrete_subtype_name_f_subtype',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_dotted_name_f_prefix = _import_func(
    'ada_dotted_name_f_prefix',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_dotted_name_f_suffix = _import_func(
    'ada_dotted_name_f_suffix',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_end_name_f_name = _import_func(
    'ada_end_name_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_end_name_p_basic_decl = _import_func(
    'ada_end_name_p_basic_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_explicit_deref_f_prefix = _import_func(
    'ada_explicit_deref_f_prefix',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_qual_expr_f_prefix = _import_func(
    'ada_qual_expr_f_prefix',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_qual_expr_f_suffix = _import_func(
    'ada_qual_expr_f_suffix',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_reduce_attribute_ref_f_prefix = _import_func(
    'ada_reduce_attribute_ref_f_prefix',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_reduce_attribute_ref_f_attribute = _import_func(
    'ada_reduce_attribute_ref_f_attribute',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_reduce_attribute_ref_f_args = _import_func(
    'ada_reduce_attribute_ref_f_args',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_char_literal_p_denoted_value = _import_func(
    'ada_char_literal_p_denoted_value',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint32)],
    ctypes.c_int
)
_string_literal_p_denoted_value = _import_func(
    'ada_string_literal_p_denoted_value',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_int_literal_p_denoted_value = _import_func(
    'ada_int_literal_p_denoted_value',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_big_integer.c_type)],
    ctypes.c_int
)
_update_attribute_ref_f_prefix = _import_func(
    'ada_update_attribute_ref_f_prefix',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_update_attribute_ref_f_attribute = _import_func(
    'ada_update_attribute_ref_f_attribute',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_update_attribute_ref_f_values = _import_func(
    'ada_update_attribute_ref_f_values',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_paren_expr_f_expr = _import_func(
    'ada_paren_expr_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_quantified_expr_f_quantifier = _import_func(
    'ada_quantified_expr_f_quantifier',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_quantified_expr_f_loop_spec = _import_func(
    'ada_quantified_expr_f_loop_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_quantified_expr_f_expr = _import_func(
    'ada_quantified_expr_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_raise_expr_f_exception_name = _import_func(
    'ada_raise_expr_f_exception_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_raise_expr_f_error_message = _import_func(
    'ada_raise_expr_f_error_message',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_un_op_f_op = _import_func(
    'ada_un_op_f_op',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_un_op_f_expr = _import_func(
    'ada_un_op_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_for_loop_iter_filter_f_expr = _import_func(
    'ada_for_loop_iter_filter_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_format_string_chunk_f_expr = _import_func(
    'ada_format_string_chunk_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_format_string_chunk_f_string_tok = _import_func(
    'ada_format_string_chunk_f_string_tok',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_format_string_chunk_p_image_subprogram = _import_func(
    'ada_format_string_chunk_p_image_subprogram',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_format_string_tok_node_p_denoted_value = _import_func(
    'ada_format_string_tok_node_p_denoted_value',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_String.c_type)],
    ctypes.c_int
)
_handled_stmts_f_stmts = _import_func(
    'ada_handled_stmts_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_handled_stmts_f_exceptions = _import_func(
    'ada_handled_stmts_f_exceptions',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_library_item_f_has_private = _import_func(
    'ada_library_item_f_has_private',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_library_item_f_item = _import_func(
    'ada_library_item_f_item',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_limited_node_p_as_bool = _import_func(
    'ada_limited_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_for_loop_spec_f_var_decl = _import_func(
    'ada_for_loop_spec_f_var_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_for_loop_spec_f_loop_type = _import_func(
    'ada_for_loop_spec_f_loop_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_for_loop_spec_f_has_reverse = _import_func(
    'ada_for_loop_spec_f_has_reverse',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_for_loop_spec_f_iter_expr = _import_func(
    'ada_for_loop_spec_f_iter_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_for_loop_spec_f_iter_filter = _import_func(
    'ada_for_loop_spec_f_iter_filter',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_while_loop_spec_f_expr = _import_func(
    'ada_while_loop_spec_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_multi_abstract_state_decl_f_decls = _import_func(
    'ada_multi_abstract_state_decl_f_decls',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_not_null_p_as_bool = _import_func(
    'ada_not_null_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_params_f_params = _import_func(
    'ada_params_f_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_paren_abstract_state_decl_f_decl = _import_func(
    'ada_paren_abstract_state_decl_f_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_pp_elsif_directive_f_expr = _import_func(
    'ada_pp_elsif_directive_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_pp_elsif_directive_f_then_kw = _import_func(
    'ada_pp_elsif_directive_f_then_kw',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_pp_if_directive_f_expr = _import_func(
    'ada_pp_if_directive_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_pp_if_directive_f_then_kw = _import_func(
    'ada_pp_if_directive_f_then_kw',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_pragma_node_f_id = _import_func(
    'ada_pragma_node_f_id',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_pragma_node_f_args = _import_func(
    'ada_pragma_node_f_args',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_pragma_node_p_is_ghost_code = _import_func(
    'ada_pragma_node_p_is_ghost_code',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_pragma_node_p_associated_entities = _import_func(
    'ada_pragma_node_p_associated_entities',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_AdaNodeArrayConverter.c_type)],
    ctypes.c_int
)
_private_node_p_as_bool = _import_func(
    'ada_private_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_protected_def_f_public_part = _import_func(
    'ada_protected_def_f_public_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_protected_def_f_private_part = _import_func(
    'ada_protected_def_f_private_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_protected_def_f_end_name = _import_func(
    'ada_protected_def_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_protected_node_p_as_bool = _import_func(
    'ada_protected_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_range_spec_f_range = _import_func(
    'ada_range_spec_f_range',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_renaming_clause_f_renamed_object = _import_func(
    'ada_renaming_clause_f_renamed_object',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_reverse_node_p_as_bool = _import_func(
    'ada_reverse_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_select_when_part_f_cond_expr = _import_func(
    'ada_select_when_part_f_cond_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_select_when_part_f_stmts = _import_func(
    'ada_select_when_part_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_stmt_p_is_ghost_code = _import_func(
    'ada_stmt_p_is_ghost_code',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_accept_stmt_f_body_decl = _import_func(
    'ada_accept_stmt_f_body_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_accept_stmt_f_entry_index_expr = _import_func(
    'ada_accept_stmt_f_entry_index_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_accept_stmt_f_params = _import_func(
    'ada_accept_stmt_f_params',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_accept_stmt_p_corresponding_entry = _import_func(
    'ada_accept_stmt_p_corresponding_entry',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_accept_stmt_with_stmts_f_stmts = _import_func(
    'ada_accept_stmt_with_stmts_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_accept_stmt_with_stmts_f_end_name = _import_func(
    'ada_accept_stmt_with_stmts_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_loop_stmt_f_spec = _import_func(
    'ada_base_loop_stmt_f_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_loop_stmt_f_stmts = _import_func(
    'ada_base_loop_stmt_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_base_loop_stmt_f_end_name = _import_func(
    'ada_base_loop_stmt_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_begin_block_f_stmts = _import_func(
    'ada_begin_block_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_begin_block_f_end_name = _import_func(
    'ada_begin_block_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_decl_block_f_decls = _import_func(
    'ada_decl_block_f_decls',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_decl_block_f_stmts = _import_func(
    'ada_decl_block_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_decl_block_f_end_name = _import_func(
    'ada_decl_block_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_case_stmt_f_expr = _import_func(
    'ada_case_stmt_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_case_stmt_f_pragmas = _import_func(
    'ada_case_stmt_f_pragmas',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_case_stmt_f_alternatives = _import_func(
    'ada_case_stmt_f_alternatives',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_extended_return_stmt_f_decl = _import_func(
    'ada_extended_return_stmt_f_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_extended_return_stmt_f_stmts = _import_func(
    'ada_extended_return_stmt_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_if_stmt_f_cond_expr = _import_func(
    'ada_if_stmt_f_cond_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_if_stmt_f_then_stmts = _import_func(
    'ada_if_stmt_f_then_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_if_stmt_f_alternatives = _import_func(
    'ada_if_stmt_f_alternatives',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_if_stmt_f_else_part = _import_func(
    'ada_if_stmt_f_else_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_named_stmt_f_decl = _import_func(
    'ada_named_stmt_f_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_named_stmt_f_stmt = _import_func(
    'ada_named_stmt_f_stmt',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_select_stmt_f_guards = _import_func(
    'ada_select_stmt_f_guards',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_select_stmt_f_else_part = _import_func(
    'ada_select_stmt_f_else_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_select_stmt_f_then_abort_part = _import_func(
    'ada_select_stmt_f_then_abort_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_abort_stmt_f_names = _import_func(
    'ada_abort_stmt_f_names',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_assign_stmt_f_dest = _import_func(
    'ada_assign_stmt_f_dest',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_assign_stmt_f_expr = _import_func(
    'ada_assign_stmt_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_call_stmt_f_call = _import_func(
    'ada_call_stmt_f_call',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_delay_stmt_f_has_until = _import_func(
    'ada_delay_stmt_f_has_until',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_delay_stmt_f_expr = _import_func(
    'ada_delay_stmt_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_exit_stmt_f_loop_name = _import_func(
    'ada_exit_stmt_f_loop_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_exit_stmt_f_cond_expr = _import_func(
    'ada_exit_stmt_f_cond_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_goto_stmt_f_label_name = _import_func(
    'ada_goto_stmt_f_label_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_label_f_decl = _import_func(
    'ada_label_f_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_raise_stmt_f_exception_name = _import_func(
    'ada_raise_stmt_f_exception_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_raise_stmt_f_error_message = _import_func(
    'ada_raise_stmt_f_error_message',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_requeue_stmt_f_call_name = _import_func(
    'ada_requeue_stmt_f_call_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_requeue_stmt_f_has_abort = _import_func(
    'ada_requeue_stmt_f_has_abort',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_return_stmt_f_return_expr = _import_func(
    'ada_return_stmt_f_return_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_simple_decl_stmt_f_decl = _import_func(
    'ada_simple_decl_stmt_f_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subunit_f_name = _import_func(
    'ada_subunit_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subunit_f_body = _import_func(
    'ada_subunit_f_body',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subunit_p_body_root = _import_func(
    'ada_subunit_p_body_root',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_synchronized_node_p_as_bool = _import_func(
    'ada_synchronized_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_tagged_node_p_as_bool = _import_func(
    'ada_tagged_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_task_def_f_interfaces = _import_func(
    'ada_task_def_f_interfaces',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_task_def_f_public_part = _import_func(
    'ada_task_def_f_public_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_task_def_f_private_part = _import_func(
    'ada_task_def_f_private_part',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_task_def_f_end_name = _import_func(
    'ada_task_def_f_end_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_then_abort_part_f_stmts = _import_func(
    'ada_then_abort_part_f_stmts',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_access_def_f_has_not_null = _import_func(
    'ada_access_def_f_has_not_null',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_access_to_subp_def_f_has_protected = _import_func(
    'ada_access_to_subp_def_f_has_protected',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_access_to_subp_def_f_subp_spec = _import_func(
    'ada_access_to_subp_def_f_subp_spec',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_anonymous_type_access_def_f_type_decl = _import_func(
    'ada_anonymous_type_access_def_f_type_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_type_access_def_f_has_all = _import_func(
    'ada_type_access_def_f_has_all',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_type_access_def_f_has_constant = _import_func(
    'ada_type_access_def_f_has_constant',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_type_access_def_f_subtype_indication = _import_func(
    'ada_type_access_def_f_subtype_indication',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_array_type_def_f_indices = _import_func(
    'ada_array_type_def_f_indices',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_array_type_def_f_component_type = _import_func(
    'ada_array_type_def_f_component_type',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_derived_type_def_f_has_abstract = _import_func(
    'ada_derived_type_def_f_has_abstract',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_derived_type_def_f_has_limited = _import_func(
    'ada_derived_type_def_f_has_limited',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_derived_type_def_f_has_synchronized = _import_func(
    'ada_derived_type_def_f_has_synchronized',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_derived_type_def_f_subtype_indication = _import_func(
    'ada_derived_type_def_f_subtype_indication',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_derived_type_def_f_interfaces = _import_func(
    'ada_derived_type_def_f_interfaces',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_derived_type_def_f_record_extension = _import_func(
    'ada_derived_type_def_f_record_extension',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_derived_type_def_f_has_with_private = _import_func(
    'ada_derived_type_def_f_has_with_private',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_enum_type_def_f_enum_literals = _import_func(
    'ada_enum_type_def_f_enum_literals',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_interface_type_def_f_interface_kind = _import_func(
    'ada_interface_type_def_f_interface_kind',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_interface_type_def_f_interfaces = _import_func(
    'ada_interface_type_def_f_interfaces',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_mod_int_type_def_f_expr = _import_func(
    'ada_mod_int_type_def_f_expr',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_private_type_def_f_has_abstract = _import_func(
    'ada_private_type_def_f_has_abstract',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_private_type_def_f_has_tagged = _import_func(
    'ada_private_type_def_f_has_tagged',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_private_type_def_f_has_limited = _import_func(
    'ada_private_type_def_f_has_limited',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_decimal_fixed_point_def_f_delta = _import_func(
    'ada_decimal_fixed_point_def_f_delta',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_decimal_fixed_point_def_f_digits = _import_func(
    'ada_decimal_fixed_point_def_f_digits',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_decimal_fixed_point_def_f_range = _import_func(
    'ada_decimal_fixed_point_def_f_range',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_floating_point_def_f_num_digits = _import_func(
    'ada_floating_point_def_f_num_digits',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_floating_point_def_f_range = _import_func(
    'ada_floating_point_def_f_range',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ordinary_fixed_point_def_f_delta = _import_func(
    'ada_ordinary_fixed_point_def_f_delta',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_ordinary_fixed_point_def_f_range = _import_func(
    'ada_ordinary_fixed_point_def_f_range',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_record_type_def_f_has_abstract = _import_func(
    'ada_record_type_def_f_has_abstract',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_record_type_def_f_has_tagged = _import_func(
    'ada_record_type_def_f_has_tagged',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_record_type_def_f_has_limited = _import_func(
    'ada_record_type_def_f_has_limited',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_record_type_def_f_record_def = _import_func(
    'ada_record_type_def_f_record_def',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_signed_int_type_def_f_range = _import_func(
    'ada_signed_int_type_def_f_range',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_type_expr_p_type_name = _import_func(
    'ada_type_expr_p_type_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_type_expr_p_designated_type_decl = _import_func(
    'ada_type_expr_p_designated_type_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_type_expr_p_designated_type_decl_from = _import_func(
    'ada_type_expr_p_designated_type_decl_from',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_type_expr_p_subtype_constraint = _import_func(
    'ada_type_expr_p_subtype_constraint',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_type_expr_p_discriminant_constraints = _import_func(
    'ada_type_expr_p_discriminant_constraints',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_ParamActualArrayConverter.c_type)],
    ctypes.c_int
)
_type_expr_p_is_definite_subtype = _import_func(
    'ada_type_expr_p_is_definite_subtype',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_type_expr_p_is_statically_constrained = _import_func(
    'ada_type_expr_p_is_statically_constrained',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_anonymous_type_f_type_decl = _import_func(
    'ada_anonymous_type_f_type_decl',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subtype_indication_f_has_not_null = _import_func(
    'ada_subtype_indication_f_has_not_null',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subtype_indication_f_name = _import_func(
    'ada_subtype_indication_f_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subtype_indication_f_constraint = _import_func(
    'ada_subtype_indication_f_constraint',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_subtype_indication_p_is_static_subtype = _import_func(
    'ada_subtype_indication_p_is_static_subtype',
    [ctypes.POINTER(_Entity_c_type),
        
        ctypes.c_uint8,
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_unconstrained_array_index_f_subtype_name = _import_func(
    'ada_unconstrained_array_index_f_subtype_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_unconstrained_array_index_f_lower_bound = _import_func(
    'ada_unconstrained_array_index_f_lower_bound',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_until_node_p_as_bool = _import_func(
    'ada_until_node_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)
_use_package_clause_f_packages = _import_func(
    'ada_use_package_clause_f_packages',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_use_type_clause_f_has_all = _import_func(
    'ada_use_type_clause_f_has_all',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_use_type_clause_f_types = _import_func(
    'ada_use_type_clause_f_types',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_value_sequence_f_iter_assoc = _import_func(
    'ada_value_sequence_f_iter_assoc',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_variant_f_choices = _import_func(
    'ada_variant_f_choices',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_variant_f_components = _import_func(
    'ada_variant_f_components',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_variant_part_f_discr_name = _import_func(
    'ada_variant_part_f_discr_name',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_variant_part_f_variant = _import_func(
    'ada_variant_part_f_variant',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_with_clause_f_has_limited = _import_func(
    'ada_with_clause_f_has_limited',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_with_clause_f_has_private = _import_func(
    'ada_with_clause_f_has_private',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_with_clause_f_packages = _import_func(
    'ada_with_clause_f_packages',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(_Entity_c_type)],
    ctypes.c_int
)
_with_private_p_as_bool = _import_func(
    'ada_with_private_p_as_bool',
    [ctypes.POINTER(_Entity_c_type),
     ctypes.POINTER(ctypes.c_uint8)],
    ctypes.c_int
)

# File readers
_dec_ref_file_reader = _import_func(
    'ada_dec_ref_file_reader',
    [_file_reader], None
)

      
_create_preprocessor_from_file = _import_func(
   "ada_create_preprocessor_from_file",
   [ctypes.c_char_p,
    ctypes.POINTER(ctypes.c_char_p),
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int)],
   _file_reader,
)



# Event handlers
_event_handler_destroy_func = ctypes.CFUNCTYPE(None, ctypes.py_object)
_event_handler_unit_requested_func = ctypes.CFUNCTYPE(
    None,
    ctypes.py_object,        # data
    AnalysisContext._c_type, # context
    ctypes.POINTER(_text),   # name
    AnalysisUnit._c_type,    # from
    ctypes.c_uint8,          # found
    ctypes.c_uint8,          # is_not_found_error
)
_event_handler_unit_parsed_func = ctypes.CFUNCTYPE(
    None,
    ctypes.py_object,        # data
    AnalysisContext._c_type, # context
    AnalysisUnit._c_type,    # unit
    ctypes.c_uint8,          # reparsed
)
_create_event_handler = _import_func(
    'ada_create_event_handler',
    [
        ctypes.py_object,
        _event_handler_destroy_func,
        _event_handler_unit_requested_func,
        _event_handler_unit_parsed_func,
    ],
    _event_handler,
)
_dec_ref_event_handler = _import_func(
    'ada_dec_ref_event_handler', [_event_handler], None
)

# Unit providers
_dec_ref_unit_provider = _import_func(
    'ada_dec_ref_unit_provider',
    [_unit_provider], None
)

      
_create_auto_provider = _import_func(
    'ada_create_auto_provider',
    [ctypes.POINTER(ctypes.c_char_p), ctypes.c_char_p],
    _unit_provider
)



# Misc
_token_get_kind = _import_func(
    "ada_token_get_kind", [Token._c_type], ctypes.c_int
)
_token_kind_name = _import_func(
    "ada_token_kind_name",
    [ctypes.c_int], ctypes.POINTER(ctypes.c_char)
)
_token_sloc_range = _import_func(
    "ada_token_sloc_range",
    [Token._c_type, ctypes.POINTER(SlocRange._c_type)], None
)
_token_next = _import_func(
    "ada_token_next",
    [Token._c_type, Token._c_type], None
)
_token_is_equivalent = _import_func(
    "ada_token_is_equivalent",
    [Token._c_type, Token._c_type], ctypes.c_int
)
_token_previous = _import_func(
    "ada_token_previous",
    [Token._c_type, Token._c_type], None
)
_token_range_text = _import_func(
    "ada_token_range_text",
    [Token._c_type, Token._c_type, ctypes.POINTER(_text)],
    ctypes.c_int
)


#
# Layering helpers
#

def _unwrap_str(c_char_p_value: Any) -> str:
    """
    Assuming c_char_p_value is a valid char*, convert it to a native Python
    string and free the C pointer.
    """
    result = ctypes.c_char_p(ctypes.addressof(c_char_p_value.contents)).value
    _free(c_char_p_value)
    return (result or b'').decode()


_kind_to_astnode_cls = {
    1: AbortAbsent,
    2: AbortPresent,
    3: AbstractAbsent,
    4: AbstractPresent,
    5: AdaNodeList,
    6: AbstractStateDeclList,
    7: AlternativesList,
    8: ConstraintList,
    9: DeclList,
    10: StmtList,
    11: AspectAssocList,
    12: BaseAssocList,
    13: AssocList,
    14: CaseExprAlternativeList,
    15: CaseStmtAlternativeList,
    16: CompilationUnitList,
    17: ConcatOperandList,
    18: ContractCaseAssocList,
    19: DefiningNameList,
    20: DiscriminantSpecList,
    21: ElsifExprPartList,
    22: ElsifStmtPartList,
    23: EnumLiteralDeclList,
    24: ExprAlternativesList,
    25: FormatStringChunkList,
    26: DiscriminantChoiceList,
    27: NameList,
    28: ParentList,
    29: ParamSpecList,
    30: PragmaNodeList,
    31: SelectWhenPartList,
    32: UnconstrainedArrayIndexList,
    33: VariantList,
    34: AliasedAbsent,
    35: AliasedPresent,
    36: AllAbsent,
    37: AllPresent,
    38: ConstrainedArrayIndices,
    39: UnconstrainedArrayIndices,
    40: AspectAssoc,
    41: AtClause,
    42: AttributeDefClause,
    43: EnumRepClause,
    44: RecordRepClause,
    45: AspectSpec,
    46: ContractCaseAssoc,
    47: PragmaArgumentAssoc,
    48: EntrySpec,
    49: EnumSubpSpec,
    50: SubpSpec,
    51: SyntheticBinarySpec,
    52: SyntheticUnarySpec,
    53: ComponentList,
    54: KnownDiscriminantPart,
    55: UnknownDiscriminantPart,
    56: EntryCompletionFormalParams,
    57: GenericFormalPart,
    58: NullRecordDef,
    59: RecordDef,
    60: AggregateAssoc,
    61: MultiDimArrayAssoc,
    62: CompositeConstraintAssoc,
    63: IteratedAssoc,
    64: ParamAssoc,
    65: AbstractStateDecl,
    66: AnonymousExprDecl,
    67: ComponentDecl,
    68: DiscriminantSpec,
    69: GenericFormalObjDecl,
    70: GenericFormalPackage,
    71: GenericFormalSubpDecl,
    72: GenericFormalTypeDecl,
    73: ParamSpec,
    74: SyntheticFormalParamDecl,
    75: GenericPackageInternal,
    76: PackageDecl,
    77: DiscreteBaseSubtypeDecl,
    78: SubtypeDecl,
    79: ClasswideTypeDecl,
    80: IncompleteTypeDecl,
    81: IncompleteFormalTypeDecl,
    82: IncompleteTaggedTypeDecl,
    83: ProtectedTypeDecl,
    84: TaskTypeDecl,
    85: SingleTaskTypeDecl,
    86: AnonymousTypeDecl,
    87: SynthAnonymousTypeDecl,
    88: ConcreteTypeDecl,
    89: FormalTypeDecl,
    90: AbstractSubpDecl,
    91: AbstractFormalSubpDecl,
    92: ConcreteFormalSubpDecl,
    93: SubpDecl,
    94: EntryDecl,
    95: EnumLiteralDecl,
    96: SyntheticCharEnumLit,
    97: GenericSubpInternal,
    98: SyntheticSubpDecl,
    99: AcceptStmtBody,
    100: ExprFunction,
    101: NullSubpDecl,
    102: SubpBody,
    103: SubpRenamingDecl,
    104: PackageBodyStub,
    105: ProtectedBodyStub,
    106: SubpBodyStub,
    107: TaskBodyStub,
    108: EntryBody,
    109: PackageBody,
    110: ProtectedBody,
    111: TaskBody,
    112: EntryIndexSpec,
    113: ErrorDecl,
    114: ExceptionDecl,
    115: ExceptionHandler,
    116: ForLoopVarDecl,
    117: GenericPackageDecl,
    118: GenericSubpDecl,
    119: GenericPackageInstantiation,
    120: GenericSubpInstantiation,
    121: GenericPackageRenamingDecl,
    122: GenericSubpRenamingDecl,
    123: LabelDecl,
    124: NamedStmtDecl,
    125: NumberDecl,
    126: ObjectDecl,
    127: ExtendedReturnStmtObjectDecl,
    128: NoTypeObjectRenamingDecl,
    129: PackageRenamingDecl,
    130: SingleProtectedDecl,
    131: SingleTaskDecl,
    132: SyntheticObjectDecl,
    133: CaseStmtAlternative,
    134: CompilationUnit,
    135: ComponentClause,
    136: ComponentDef,
    137: ConstantAbsent,
    138: ConstantPresent,
    139: CompositeConstraint,
    140: DeltaConstraint,
    141: DigitsConstraint,
    142: RangeConstraint,
    143: DeclarativePart,
    144: PrivatePart,
    145: PublicPart,
    146: ElsePart,
    147: ElsifExprPart,
    148: ElsifStmtPart,
    149: AbstractStateDeclExpr,
    150: Allocator,
    151: Aggregate,
    152: BracketAggregate,
    153: DeltaAggregate,
    154: BracketDeltaAggregate,
    155: NullRecordAggregate,
    156: BinOp,
    157: RelationOp,
    158: BoxExpr,
    159: CaseExprAlternative,
    160: ConcatOp,
    161: ConcatOperand,
    162: CaseExpr,
    163: IfExpr,
    164: ContractCases,
    165: DeclExpr,
    166: FormatStringLiteral,
    167: MembershipExpr,
    168: ArraySubcomponentChoiceName,
    169: AttributeRef,
    170: CallExpr,
    171: DefiningName,
    172: SyntheticDefiningName,
    173: DiscreteSubtypeName,
    174: DottedName,
    175: EndName,
    176: ExplicitDeref,
    177: QualExpr,
    178: ReduceAttributeRef,
    179: CharLiteral,
    180: Identifier,
    181: OpAbs,
    182: OpAnd,
    183: OpAndThen,
    184: OpConcat,
    185: OpDiv,
    186: OpDoubleDot,
    187: OpEq,
    188: OpGt,
    189: OpGte,
    190: OpIn,
    191: OpLt,
    192: OpLte,
    193: OpMinus,
    194: OpMod,
    195: OpMult,
    196: OpNeq,
    197: OpNot,
    198: OpNotIn,
    199: OpOr,
    200: OpOrElse,
    201: OpPlus,
    202: OpPow,
    203: OpRem,
    204: OpXor,
    205: StringLiteral,
    206: NullLiteral,
    207: IntLiteral,
    208: RealLiteral,
    209: SyntheticIdentifier,
    210: TargetName,
    211: UpdateAttributeRef,
    212: ParenExpr,
    213: QuantifiedExpr,
    214: RaiseExpr,
    215: UnOp,
    216: ForLoopIterFilter,
    217: FormatStringChunk,
    218: FormatStringTokEnd,
    219: FormatStringTokMid,
    220: FormatStringTokStart,
    221: FormatStringTokString,
    222: HandledStmts,
    223: InterfaceKindLimited,
    224: InterfaceKindProtected,
    225: InterfaceKindSynchronized,
    226: InterfaceKindTask,
    227: IterTypeIn,
    228: IterTypeOf,
    229: LibraryItem,
    230: LimitedAbsent,
    231: LimitedPresent,
    232: ForLoopSpec,
    233: WhileLoopSpec,
    234: ModeDefault,
    235: ModeIn,
    236: ModeInOut,
    237: ModeOut,
    238: MultiAbstractStateDecl,
    239: NotNullAbsent,
    240: NotNullPresent,
    241: NullComponentDecl,
    242: OthersDesignator,
    243: OverridingNotOverriding,
    244: OverridingOverriding,
    245: OverridingUnspecified,
    246: Params,
    247: ParenAbstractStateDecl,
    248: PpElseDirective,
    249: PpElsifDirective,
    250: PpEndIfDirective,
    251: PpIfDirective,
    252: PpThenKw,
    253: PragmaNode,
    254: PrivateAbsent,
    255: PrivatePresent,
    256: ProtectedDef,
    257: ProtectedAbsent,
    258: ProtectedPresent,
    259: QuantifierAll,
    260: QuantifierSome,
    261: RangeSpec,
    262: RenamingClause,
    263: SyntheticRenamingClause,
    264: ReverseAbsent,
    265: ReversePresent,
    266: SelectWhenPart,
    267: AcceptStmt,
    268: AcceptStmtWithStmts,
    269: ForLoopStmt,
    270: LoopStmt,
    271: WhileLoopStmt,
    272: BeginBlock,
    273: DeclBlock,
    274: CaseStmt,
    275: ExtendedReturnStmt,
    276: IfStmt,
    277: NamedStmt,
    278: SelectStmt,
    279: ErrorStmt,
    280: AbortStmt,
    281: AssignStmt,
    282: CallStmt,
    283: DelayStmt,
    284: ExitStmt,
    285: GotoStmt,
    286: Label,
    287: NullStmt,
    288: RaiseStmt,
    289: RequeueStmt,
    290: ReturnStmt,
    291: SimpleDeclStmt,
    292: TerminateAlternative,
    293: SubpKindFunction,
    294: SubpKindProcedure,
    295: Subunit,
    296: SynchronizedAbsent,
    297: SynchronizedPresent,
    298: TaggedAbsent,
    299: TaggedPresent,
    300: TaskDef,
    301: ThenAbortPart,
    302: TypeAttributesRepository,
    303: AccessToSubpDef,
    304: AnonymousTypeAccessDef,
    305: TypeAccessDef,
    306: ArrayTypeDef,
    307: DerivedTypeDef,
    308: EnumTypeDef,
    309: FormalDiscreteTypeDef,
    310: InterfaceTypeDef,
    311: ModIntTypeDef,
    312: PrivateTypeDef,
    313: DecimalFixedPointDef,
    314: FloatingPointDef,
    315: OrdinaryFixedPointDef,
    316: RecordTypeDef,
    317: SignedIntTypeDef,
    318: AnonymousType,
    319: EnumLitSynthTypeExpr,
    320: SubtypeIndication,
    321: ConstrainedSubtypeIndication,
    322: DiscreteSubtypeIndication,
    323: SyntheticTypeExpr,
    324: UnconstrainedArrayIndex,
    325: UntilAbsent,
    326: UntilPresent,
    327: UsePackageClause,
    328: UseTypeClause,
    329: ValueSequence,
    330: Variant,
    331: VariantPart,
    332: WithClause,
    333: WithPrivateAbsent,
    334: WithPrivatePresent,
}


def _field_address(struct: ctypes.Structure, field_name: str) -> int:
    """
    Get the address of a structure field from a structure value.

    For instance::

        class Foo(ctypes.Structure):
            _fields_ = [('i', ctypes.c_int)]

        f = Foo()
        i_addr =_field_address(f, 'i')
    """
    struct_type = type(struct)
    struct_addr = ctypes.addressof(struct)
    field = getattr(struct_type, field_name)
    field_type = None
    for field_desc in struct_type._fields_:
        f_name = field_desc[0]
        f_type = field_desc[1]
        if f_name == field_name:
            field_type = f_type
            break
    assert field_type is not None
    return struct_addr + field.offset

def _extract_versions() -> Tuple[str, str]:
    v_ptr = ctypes.c_char_p()
    bd_ptr = ctypes.c_char_p()
    _get_versions(ctypes.byref(v_ptr), ctypes.byref(bd_ptr))

    _version = v_ptr.value
    assert isinstance(_version, bytes)
    version = _version.decode()
    _free(v_ptr)

    _build_version = bd_ptr.value
    assert isinstance(_build_version, bytes)
    build_version = _build_version.decode()
    _free(bd_ptr)

    return version, build_version

version, build_date = _extract_versions()


#
# Language specific extensions #
#


      
def token_match(self, other):
    """
    Helper for the finditer/find/findall methods, so that a token matches
    another token even if they are not strictly equivalent.
    """
    return self == other or self.text == other


def _coerce_bytes(label, value, what='a bytes string', or_none=False):
    """
    Take bytes (forwarded as-is to C) but also accept text (encoded using
    the system encoding).
    """
    if value is None and or_none:
        return None
    elif isinstance(value, bytes):
        return value
    elif isinstance(value, str):
        return value.encode()
    else:
        raise TypeError('`{}` argument must be {} (got {})'
                        .format(label, what, _type_fullname(type(value))))


_set_config_pragmas_mapping = _import_func(
    "ada_set_config_pragmas_mapping",
    [AnalysisContext._c_type,
     AnalysisUnit._c_type,
     ctypes.POINTER(AnalysisUnit._c_type)],
    None
)


class TargetInformation:
    """
    Built in types characteristics for a given target platform.
    """

    def __init__(self, filename: str):
        """
        Read target information from ``filename`` and return it. Raise an
        ``Langkit_Support.Errors.Invalid_Input`` exception if it cannot be read
        of if there is any trouble decoding it.
        """

        # First, define this attribute so that __del__ work even if the
        # constructor aborts later on because of an exception.
        self._c_value = None

        c_filename = _coerce_bytes("filename", filename)
        self._c_value = self._c_load(c_filename)

    def __del__(self):
        if self._c_value is not None:
            self._c_free(self._c_value)

    _c_type = _hashable_c_pointer()

    _c_load = staticmethod(_import_func(
        "ada_target_info_load", [ctypes.c_char_p], _c_type,
    ))
    _c_free = staticmethod(
        _import_func("ada_target_info_free", [_c_type], None)
    )
    _c_set = staticmethod(
        _import_func(
            "ada_target_info_set", [AnalysisContext._c_type, _c_type], None
        )
    )



class _c_string_array(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_int),
        ("c_ptr", ctypes.POINTER(ctypes.c_char_p)),
        # Omit the "items" field: it has variable size and is not necessary
        # to just read the items.
    ]

    @property
    def wrap(self) -> List[str]:
        return [self.c_ptr[i] for i in range(self.length)]

_c_string_array_ptr = ctypes.POINTER(_c_string_array)

_c_free_string_array = _import_func(
    "ada_free_string_array", [_c_string_array_ptr], None,
)


@property
def doc_name(n):
    """
    Format this name to be a readable qualified name for the entity designated
    by it. Meant to be used in documentation context.

    If the entity is local, it will return the relative name. If it is
    non-local, return the shortest qualified name not taking use clauses into
    account.

    .. WARNING:: This is an EXPERIMENTAL feature. This is a python specific
        method, because for the moment this is not conveniently implementable
        directly as a libadalang property.  Consider it an experimental API
        endpoint, and use it at your own risk.
    """
    if n.p_is_defining and not n.is_a(DefiningName):
        n = n.p_enclosing_defining_name

    ref_decl = n.p_basic_decl if n.p_is_defining else n.p_referenced_decl()
    ref_decl_fqn = ref_decl.p_fully_qualified_name

    enclosing_package = next(
        (p for p in n.parents() if p.is_a(BasePackageDecl)),
        None
    )

    if enclosing_package is None or enclosing_package == ref_decl:
        return ref_decl_fqn

    enclosing_decl_fqn = enclosing_package.p_fully_qualified_name

    if ref_decl_fqn.lower().startswith(enclosing_decl_fqn.lower()):
        return ref_decl_fqn[len(enclosing_decl_fqn):].strip(".")
    else:
        return ref_decl_fqn

Token.match = token_match
Name.doc_name = doc_name


import enum
class SourceFilesMode(enum.Enum):
    """
    Mode to get a list of source files from a project file.

    See ``SourceFiles.for_project``.
    """
    default = 0
    root_project = 1
    whole_project = 2
    whole_project_with_runtime = 3


class GPRProject:
    """
    Load a GPR project file.
    """

    class _UnitProvider(UnitProvider):
        def __init__(self, project: GPRProject, c_value: Any):
            super().__init__(c_value)

            # Keep a reference on the GPRProject instance that was used to
            # create this unit provider so that the project lives at least as
            # long as the unit provider.
            self._project = project

    def __init__(self,
                 project_file: str,
                 scenario_vars: Dict[str, str] = {},
                 target: Opt[str] = None,
                 runtime: Opt[str] = None,
                 config_file: Opt[str] = None,
                 ada_only: bool = False,
                 print_errors: bool = True):
        """
        Load a GPR project file.

        This may raise an ``InvalidProjectError`` exception if an error occurs
        when loading the project.

        :param project_file: Filename for the project to load.
        :param screnario_vars: External variables for the project to load.
        :param target: Name of the target for the project to load. Assume the
            native platform if left to None.
        :param runtime: Name of the runtime for the project to load. Use the
            default runtime for the selected target if left to None.
        :param config_file: Name of the GPR configuration file for the project
            to load. Use autoconfiguration if left to None.
        :param ada_only: Whether to restrict GPR file loading to the Ada
            language.
        :param print_errors: Whether to print non-critical error messages
            emitted during project loading on the standard error stream. See
            the ``errors`` method to have programmatic access to the list of
            errors.
        """
        # First, define this attribute so that __del__ work even if the
        # constructor aborts later on because of an exception.
        self._c_value = None

        # Turn arguments into C API values
        c_project_file = _coerce_bytes('project_file', project_file)
        c_target = _coerce_bytes('target', target, or_none=True)
        c_runtime = _coerce_bytes('runtime', runtime, or_none=True)
        c_config_file = _coerce_bytes('config_file', config_file, or_none=True)

        if scenario_vars:
            items = scenario_vars.items()
            scn_vars_array_type = (
                self._c_scenario_variable * (len(items) + 1)
            )
            c_scenario_vars = scn_vars_array_type()
            for i, (name, value) in enumerate(items):
                what = 'a dict mapping bytes strings to bytes strings'
                name = _coerce_bytes('scenario_vars', name, what)
                value = _coerce_bytes('scenario_vars', value, what)
                c_scenario_vars[i] = self._c_scenario_variable(
                    name, value
                )
            c_scenario_vars[-1] = self._c_scenario_variable(None, None)
        else:
            c_scenario_vars = None

        # Load the project
        c_errors = _c_string_array_ptr()
        c_project = self._c_type()
        self._c_load(
            c_project_file,
            c_scenario_vars,
            c_target,
            c_runtime,
            c_config_file,
            int(ada_only),
            ctypes.byref(c_project),
            ctypes.byref(c_errors),
        )
        self._c_value = c_project

        # Extract the possibly empty list of error messages and print it if
        # requested. For user convenience, convert error messages to unicode
        # strings and discard decoding errors.
        self._errors = [
            msg.decode("utf-8", "replace") for msg in c_errors.contents.wrap
        ]
        _c_free_string_array(c_errors)
        if print_errors and self.errors:
            print(f"Errors while loading {project_file}:", file=sys.stderr)
            for e in self.errors:
                print(e, file=sys.stderr)

    def __del__(self):
        if self._c_value is not None:
            self._c_free(self._c_value)

    @property
    def errors(self) -> List[str]:
        """
        Possibly empty list of non-critical error messages emitted during
        project loading.
        """
        return list(self._errors)

    def create_unit_provider(self, project: Opt[str] = None) -> UnitProvider:
        """
        Return a unit provider that uses this GPR project.

        :param project: If None, let the unit provider use the whole project
            tree. Otherwise, restrict the unit provider to the project with the
            given name in the project tree.

            As unit providers must guarantee that there exists at most one
            source file for each couple (unit name, unit kind), aggregate
            projects that contains several conflicting units are not supported:
            trying to use one will yield an ``InvalidProjectError`` exception.
        """
        c_project = _coerce_bytes('project', project, or_none=True)
        c_value = self._c_create_unit_provider(self._c_value, c_project)
        return self._UnitProvider(self, c_value)

    def source_files(
        self,
        mode: SourceFilesMode = SourceFilesMode.default,
        projects: List[str] = [],
    ):
        """
        Return the list of source files in this project according to ``mode``:

        * ``default``: sources in the root project and its non-externally built
          dependencies;

        * ``root_project``: sources in the root project only;

        * ``whole_project``: sources in the whole project tree (i.e. including
          externally built dependencies);

        * ``whole_project_with_runtime``: sources in the whole project tree
          plus runtime sources.

        If ``projects`` is not empty, return instead the list for the sources
        in all the sub-projects that ``projects`` designates, still applying
        the given mode to the search.
        """

        assert isinstance(mode, SourceFilesMode)
        c_mode = mode.value

        projects_type = ctypes.c_char_p * len(projects)
        projects_c_strings = [
            ctypes.c_char_p(_coerce_bytes("projects", p)) for p in projects
        ]
        c_projects = projects_type(*projects_c_strings)

        # Compute the list of source files, extract it (no error expected there
        # unless we have a bug) and free the resources.
        c_value = self._c_source_files(
            self._c_value, c_mode, c_projects, len(projects)
        )
        assert c_value
        result = c_value.contents.wrap
        _c_free_string_array(c_value)

        # Now convert filenames to Unicode strings using the system default
        # encoding, to be more consistent with other Python APIs.
        return [f.decode() for f in result]

    def default_charset(self, project: Opt[str] = None) -> str:
        """
        Try to detect the default charset to use for the given project.

        Restrict the detection to the subproject ``project``, or to ``self``'s
        root project if ``project`` is left to ``None``.

        Note that, as of today, this detection only looks for the ``-gnatW8``
        compiler switch: other charsets are not supported.
        """
        c_project = _coerce_bytes('project', project, or_none=True)
        result = self._c_default_charset(self._c_value, c_project)
        return _unwrap_str(result)

    def create_preprocessor(
        self,
        project: Opt[str] = None,
        line_mode: Optional[FileReader.LineMode] = None,
    ) -> FileReader:
        """
        Create preprocessor data from compiler arguments found in the given GPR
        project (``-gnatep`` and ``-gnateD`` arguments), or from the
        ``project`` sub-project (if the argument is passed).

        Note that this function collects all arguments and returns an
        approximation from them: it does not replicates exactly gprbuild's
        behavior. This may raise a ``File_Read_Error`` exception if this fails
        to read a preprocessor data file and a ``Syntax_Error`` exception if
        one such file has invalid syntax.
        """
        c_project = _coerce_bytes('project', project, or_none=True)

        if line_mode is None:
            c_line_mode_ref = None
        else:
            c_line_mode = ctypes.c_int(FileReader.LineMode._unwrap(line_mode))
            c_line_mode_ref = ctypes.byref(c_line_mode)

        return FileReader(
            self._c_create_preprocessor(
                self._c_value, c_project, c_line_mode_ref
            ),
        )

    def create_context(
        self,
        project: Opt[str] = None,
        event_handler: Opt[EventHandler] = None,
        with_trivia: bool = True,
        tab_stop: int = 8,
    ) -> AnalysisContext:
        """
        Wrapper around ``Initialize_Context_From_Project`` to initialize
        ``Context`` (an already allocated but not yet initialized analysis
        context) from ``Self``.
        """
        c_project = _coerce_bytes("project", project, or_none=True)

        event_handler_wrapper, c_event_handler = (
            _EventHandlerWrapper.create(event_handler)
        )

        # Manually allocate a C-level analysis context so that we can
        # initialize it ourselves.
        c_value = _allocate_analysis_context()

        # Create the Python wrapper, so that we have one ready for event
        # handler callbacks triggered during context initialization.
        result = AnalysisContext(_c_value=c_value)

        # The wrapper created its own ownership share: release ours
        _context_decref(c_value)

        # Attach extra wrappers to the analysis context wrapper so that
        # wrappers live at least as long as the analysis context.
        #
        # HACK: use the "_unit_provider" slot for the GPRProject instance
        # itself. The initialization procedure will take care of creating a
        # unit provider and make the context actually use it: there is no need
        # for a UnitProvider wrapper here.
        result._event_handler_wrapper = event_handler_wrapper
        result._unit_provider = self

        # Finally, initialize the analysis context. Note that this step may
        # raise an exception: in that case, the analysis context is considered
        # not initialized, but we are not returning "result", so it will become
        # unreachable from the Python world, and garbage collection will take
        # care of releasing it.
        self._c_initialize_context(
            self._c_value,
            c_value,
            c_project,
            c_event_handler,
            with_trivia,
            tab_stop,
        )

        return result

    _c_type = _hashable_c_pointer()

    class _c_scenario_variable(ctypes.Structure):
        _fields_ = [('name', ctypes.c_char_p),
                    ('value', ctypes.c_char_p)]

    _c_load = staticmethod(_import_func(
        "ada_gpr_project_load",
        [ctypes.c_char_p,
         ctypes.POINTER(_c_scenario_variable),
         ctypes.c_char_p,
         ctypes.c_char_p,
         ctypes.c_char_p,
         ctypes.c_int,
         ctypes.POINTER(_c_type),
         ctypes.POINTER(_c_string_array_ptr)],
        None,
    ))

    _c_free = staticmethod(
        _import_func("ada_gpr_project_free", [_c_type], None)
    )

    _c_create_unit_provider = staticmethod(_import_func(
        "ada_gpr_project_create_unit_provider",
        [_c_type, ctypes.c_char_p],
        _unit_provider,
    ))

    _c_source_files = staticmethod(_import_func(
        "ada_gpr_project_source_files",
        [_c_type, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.c_int],
        _c_string_array_ptr,
    ))

    _c_default_charset = staticmethod(_import_func(
        "ada_gpr_project_default_charset",
        [_c_type, ctypes.c_char_p], ctypes.POINTER(ctypes.c_char),
    ))

    _c_create_preprocessor = staticmethod(_import_func(
        "ada_gpr_project_create_preprocessor",
        [_c_type, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)],
        _file_reader,
    ))

    _c_initialize_context = staticmethod(_import_func(
        "ada_gpr_project_initialize_context",
        [_c_type,                  # gpr_project
         AnalysisContext._c_type, # context
         ctypes.c_char_p,         # project
         _event_handler,          # event_handler
         ctypes.c_int,            # with_trivia
         ctypes.c_int],           # tab_stop
        None,
    ))



#
# App base class
#

class App:
    """
    Base class to regroup logic for an app. We use a class so that
    specific languages implementations can add specific arguments and
    processing by overriding specific methods:

    - `main`, which will be the main method of the app.

    - `add_arguments` to add arguments to the argparse.Parser instance

    - `create_unit_provider` to return a custom unit provider to be used by the
      AnalysisContext.

    - `description` to change the description of the app.

    Inside of `main`, the user can access app specific state:

    - `self.units` is a map of filenames to analysis units.
    - `self.ctx` is the analysis context.
    - `self.u` is the last parsed unit.

    The user can then run the app by calling `App.run()`.

    Here is a small example of an app subclassing `App`, that will simply print
    the tree of every unit passed as argument:

    .. code-block:: python

        from libadalang import App


        class ExampleApp(App):
            def main(self):
                for u in self.units.values():
                    print u.filename
                    print u.root.dump()

        ExampleApp.run()
    """

    parser: argparse.ArgumentParser
    args: argparse.Namespace
    u: AnalysisUnit
    units: Dict[str, AnalysisUnit]
    ctx: AnalysisContext

    @property
    def description(self) -> str:
        """
        Description for this app. Empty by default.
        """
        return ""

    def __init__(self, args: Opt[List[str]] = None):
        self.parser = argparse.ArgumentParser(description=self.description)
        self.parser.add_argument('files', nargs='*', help='Files')
        self.add_arguments()

        # Parse command line arguments
        self.args = self.parser.parse_args(args)

        self.ctx = AnalysisContext(
            charset='utf-8',
            unit_provider=self.create_unit_provider(),
            event_handler=self.create_event_handler(),
            with_trivia=True,
        )

        files = self.args.files
        if not files:
            files = self.default_get_files()

        # Parse files
        self.units = {}
        for file_name in files:
            self.u = self.ctx.get_from_file(file_name)
            if self.u.diagnostics:
                self.on_parsing_errors(self.u)
            self.units[file_name] = self.u

    def on_parsing_errors(self, unit: AnalysisUnit) -> None:
        """
        Callback invoked during App initialization, when a requested unit has a
        parsing error. By default, print the error on the standard output, but
        subclasses can override this behavior.
        """
        for d in unit.diagnostics:
            print(unit.format_gnu_diagnostic(d))

    def default_get_files(self) -> List[str]:
        """
        When no files are passed by the user on the command line, this method
        will be used. By default, it returns an empty list, but Langkit
        libraries can overload it to customize the behavior to adapt it to the
        specific language and toolchain.
        """
        return []

    def add_arguments(self) -> None:
        """
        Hook for subclasses to add arguments to self.parser. Default
        implementation does nothing.
        """
        pass

    def create_unit_provider(self) -> Opt[UnitProvider]:
        """
        Hook for subclasses to return a custom unit provider.
        Default implementation returns None.
        """
        return None

    def create_event_handler(self) -> Opt[EventHandler]:
        """
        Hook for subclasses to return a custom event handler. Default
        implementation returns None.
        """
        return None

    def main(self) -> None:
        """
        Default implementation for App.main: just iterates on every units and
        call ``process_unit`` on it.
        """
        for u in sorted(self.units.values(), key=lambda u: u.filename):
            self.process_unit(u)

    def process_unit(self, unit: AnalysisUnit) -> None:
        """
        Abstract method that processes one unit. Needs to be subclassed by
        implementors.
        """
        raise NotImplementedError()

    @classmethod
    def run(cls, args: Opt[List[str]]=None) -> None:
        """
        Instantiate and run this application.
        """
        cls(args).main()

    
      

    def add_arguments(self):
        self.parser.add_argument(
            '-X', action='append',
            help="Scenario variables to pass along to GPR"
        )
        self.parser.add_argument(
            '-P', '--project', type=str, default='', help="GPR project file"
        )
        self.parser.add_argument(
            "--subproject",
            dest="subprojects",
            default=[],
            action="append",
            help="If passed, list of subprojects in which to look for source"
            " files. If not passed, start from the root project only."
        )
        self.parser.add_argument(
            "-U",
            "--recursive",
            dest="process_full_project_tree",
            action="store_true",
            help="Process all units in the project tree, excluding externally"
            " built projects unless the --process-runtime option is also"
            " passed.",
        )
        self.parser.add_argument(
            "--process-runtime",
            action="store_true",
            help= "Process the runtime files, and any other predefined"
            " sources.",
        )
        self.parser.add_argument(
            '-k', '--keep-going-on-missing-file', action='store_true',
            help="Behavior when encountering missing files. By default, exit"
                 " with an error on the first missing dependency. Continue"
                 " with a warning in the option is passed."
        )

    def create_unit_provider(self):
        if not self.args.project:
            self.project = None
            return None

        self.scenario_vars = {}
        if self.args.X:
            for var in self.args.X:
                k, v = var.split("=")
                self.scenario_vars[k] = v
        self.project = GPRProject(
            self.args.project, scenario_vars=self.scenario_vars
        )
        return self.project.create_unit_provider()

    def default_get_files(self):
        # If a project was loaded, process its source files. Otherwise, process
        # no source file by default.
        if self.project is None:
            return []

        if self.args.process_runtime:
            mode = SourceFilesMode.whole_project_with_runtime
        elif self.args.process_full_project_tree:
            mode = SourceFilesMode.default
        else:
            mode = SourceFilesMode.root_project

        return self.project.source_files(mode, self.args.subprojects)

    def create_event_handler(self) -> Opt[EventHandler]:
        return self.CommandLineEventHandler(
            self.args.keep_going_on_missing_file
        )

    class CommandLineEventHandler(EventHandler):
        """
        Event handler to warn for each missing file.
        """

        def __init__(self, keep_going_on_missing_file: bool):
            self.keep_going_on_missing_file = keep_going_on_missing_file
            self.already_seen_missing_files: Set[str] = set()

        def unit_requested_callback(self,
                                    context: AnalysisContext,
                                    name: str,
                                    from_unit: AnalysisUnit,
                                    found: bool,
                                    is_not_found_error: bool) -> None:
            # Warn only about missing files that are needed according to Ada
            # legality rules.
            if (
                found
                or not is_not_found_error
                or name in self.already_seen_missing_files
            ):
                return

            self.already_seen_missing_files.add(name)
            self.report_missing_file(name)

        def report_missing_file(self, filename: str) -> None:
            basename = os.path.basename(filename)
            prefix = "WARNING" if self.keep_going_on_missing_file else "ERROR"
            print(f"{prefix}: File {basename} not found")
            if not self.keep_going_on_missing_file:
                # This is a callback from the C world, so propagating a
                # SystemExit exception like ``sys.exit`` does is not going to
                # work. Use the OS-level exit system call instead to avoid
                # relying on exception propagation.
                #
                # Since we use the system call to exit, standard streams need
                # to be manually flushed so that buffered content is written
                # before the exit.
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(1)


