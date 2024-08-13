"""
Parses MuJoCo header files and generates Python bindings.

Licensed under the Apache License, Version 2.0. See the LICENSE file for details.
"""

import os
import pprint
import textwrap

from absl import logging
from dm_control.autowrap import codegen_util
from dm_control.autowrap import header_parsing
import pyparsing


class BindingGenerator:
    """Parses declarations from MuJoCo headers and generates Python bindings."""

    def __init__(self, enums_dict=None, consts_dict=None, typedefs_dict=None, hints_dict=None, index_dict=None):
        """
        Initializes a new BindingGenerator instance.

        Args:
            enums_dict: Nested mappings from {enum_name: {member_name: value}}.
            consts_dict: Mapping from {const_name: value}.
            typedefs_dict: Mapping from {type_name: ctypes_typename}.
            hints_dict: Mapping from {var_name: shape_tuple}.
            index_dict: Mapping from {lowercase_struct_name: {var_name: shape_tuple}}.
        """
        self.enums_dict = enums_dict or codegen_util.UniqueOrderedDict()
        self.consts_dict = consts_dict or codegen_util.UniqueOrderedDict()
        self.typedefs_dict = typedefs_dict or codegen_util.UniqueOrderedDict()
        self.hints_dict = hints_dict or codegen_util.UniqueOrderedDict()
        self.index_dict = index_dict or codegen_util.UniqueOrderedDict()

    def get_consts_and_enums(self):
        """Combines constants and enum members into a single dictionary."""
        consts_and_enums = self.consts_dict.copy()
        for enum in self.enums_dict.values():
            consts_and_enums.update(enum)
        return consts_and_enums

    def resolve_size(self, old_size):
        """Resolves an array size identifier to an integer or string."""
        if isinstance(old_size, int):
            return old_size
        if "*" in old_size:
            sizes = [self.resolve_size(part) for part in old_size.split("*")]
            if all(isinstance(dim, int) for dim in sizes):
                return int(np.prod(sizes))
            return tuple(sizes)
        size = codegen_util.recursive_dict_lookup(old_size, self.get_consts_and_enums())
        return codegen_util.try_coerce_to_num(size, try_types=(int,)) or old_size

    def get_shape_tuple(self, old_size, squeeze=False):
        """Generates a shape tuple from parser results."""
        if isinstance(old_size, pyparsing.ParseResults):
            shape = tuple(self.resolve_size(dim) for dim in old_size)
        else:
            shape = (self.resolve_size(old_size),)
        return tuple(d for d in shape if not squeeze or d != 1)

    def resolve_typename(self, old_ctypes_typename):
        """Gets a qualified ctypes typename from typedefs_dict and C_TO_CTYPES."""
        new_ctypes_typename = codegen_util.recursive_dict_lookup(old_ctypes_typename, self.typedefs_dict)
        new_ctypes_typename = header_parsing.C_TO_CTYPES.get(new_ctypes_typename, new_ctypes_typename)

        if new_ctypes_typename == old_ctypes_typename:
            logging.warning("Could not resolve typename '%s'", old_ctypes_typename)

        return new_ctypes_typename

    def parse_hints(self, xmacro_src):
        """Parses mjxmacro.h, updating self.hints_dict."""
        parser = header_parsing.XMACRO
        for tokens, _, _ in parser.scanString(xmacro_src):
            for xmacro in tokens:
                for member in xmacro.members:
                    shape = self.get_shape_tuple(member.dims, squeeze=True)
                    self.hints_dict[member.name] = shape

                    if codegen_util.is_macro_pointer(xmacro.name):
                        struct_name = codegen_util.macro_struct_name(xmacro.name)
                        self.index_dict.setdefault(struct_name, {})[member.name] = shape

    def parse_enums(self, src):
        """Parses mj*.h, updating self.enums_dict."""
        parser = header_parsing.ENUM_DECL
        for tokens, _, _ in parser.scanString(src):
            for enum in tokens:
                members = codegen_util.UniqueOrderedDict()
                value = 0
                for member in enum.members:
                    if member.bit_lshift_a:
                        value = int(member.bit_lshift_a) << int(member.bit_lshift_b)
                    elif member.value:
                        value = int(member.value)
                    else:
                        value += 1
                    members[member.name] = value
                self.enums_dict[enum.name] = members

    def parse_consts_typedefs(self, src):
        """Updates self.consts_dict, self.typedefs_dict."""
        parser = header_parsing.COND_DECL | header_parsing.UNCOND_DECL
        for tokens, _, _ in parser.scanString(src):
            self._recurse_into_conditionals(tokens)

    def _recurse_into_conditionals(self, tokens):
        """Handles nested #if(n)def... #else... #endif blocks."""
        for token in tokens:
            if token.predicate:
                condition = self.get_consts_and_enums().get(token.predicate)
                if condition:
                    self._recurse_into_conditionals(token.if_true)
                else:
                    self._recurse_into_conditionals(token.if_false)
            elif token.typename:
                self.typedefs_dict[token.name] = token.typename
            elif token.value:
                value = codegen_util.try_coerce_to_num(token.value)
                if not isinstance(value, str):
                    self.consts_dict[token.name] = value
            else:
                self.consts_dict[token.name] = True

    def make_header(self, imports=()):
        """Returns a header string for an auto-generated Python source file."""
        docstring = textwrap.dedent(f"""
        \"\"\"Automatically generated by {os.path.split(__file__)[-1]}.

        MuJoCo header version: {self.consts_dict.get("mjVERSION_HEADER", "Unknown")}
        \"\"\"
        """).strip()
        return "\n".join([docstring] + list(imports) + ["\n"])

    def write_consts(self, fname):
        """Writes constants to a Python file."""
        imports = ["# pylint: disable=invalid-name"]
        with open(fname, "w") as f:
            f.write(self.make_header(imports))
            f.write(codegen_util.comment_line("Constants") + "\n")
            for name, value in self.consts_dict.items():
                f.write(f"{name} = {value}\n")
            f.write("\n" + codegen_util.comment_line("End of generated code"))

    def write_enums(self, fname):
        """Writes enum definitions to a Python file."""
        imports = ["import collections", "# pylint: disable=invalid-name", "# pylint: disable=line-too-long"]
        with open(fname, "w") as f:
            f.write(self.make_header(imports))
            f.write(codegen_util.comment_line("Enums"))
            for enum_name, members in self.enums_dict.items():
                fields = ", ".join(f"\"{name}\"" for name in members.keys())
                values = ", ".join(str(value) for value in members.values())
                f.write(textwrap.dedent(f"""
                {enum_name} = collections.namedtuple(
                    "{enum_name}",
                    [{fields}]
                )({values})
                """))
            f.write("\n" + codegen_util.comment_line("End of generated code"))

    def write_index_dict(self, fname):
        """Writes array shape information for indexing to a Python file."""
        imports = ["# pylint: disable=bad-continuation", "# pylint: disable=line-too-long"]
        with open(fname, "w") as f:
            f.write(self.make_header(imports))
            f.write("array_sizes = (\n")
            f.write(pprint.pformat(dict(self.index_dict)))
            f.write("\n)")
            f.write("\n" + codegen_util.comment_line("End of generated code"))
