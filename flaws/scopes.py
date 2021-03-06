import ast
from collections import defaultdict, deque

from funcy import cached_property, print_exits, any

from .asttools import nodes_str, is_write



# TODO: distinguish python versions
import __builtin__
BUILTINS = set(dir(__builtin__))


class Scope(object):
    def __init__(self, parent, node):
        self.parent = parent
        if parent:
            parent.children.append(self)
        self.children = []

        self.node = node
        self.names = defaultdict(list)
        self.unscoped_names = defaultdict(list)
        self.global_names = set()

    @cached_property
    def module(self):
        scope = self
        while not scope.is_module:
            scope = scope.parent
        return scope

    @property
    def is_module(self):
        return isinstance(self.node, ast.Module)

    @property
    def is_class(self):
        return isinstance(self.node, ast.ClassDef)

    @cached_property
    @print_exits
    def exports(self):
        # There are several possible scenarious:
        #   1. Explicit exports
        #   2. No explicit exports, using _ prefix?
        #   3. Failed to parse __all__
        #   4. Not a module - same as __all__ = []?
        # We treat 3 as 2 for now.
        if not self.is_module:
            return []
        if '__all__' not in self.names:
            return None


        exports_node = self.names['__all__'][0]
        assign = exports_node.up
        if not isinstance(assign, ast.Assign) or len(assign.targets) != 1:
            print "WARN: failed parsing __all__"
            return None

        try:
            return ast_eval(assign.value)
        except ValueError:
            print "WARN: failed parsing __all__"
            return None

    # Names

    def add(self, name, node):
        # Params are always in current scope.
        # Other names could be made global or passed to parent scope upon exit,
        # unless we are in a module.
        is_param = isinstance(node, ast.Name) and isinstance(node.ctx, ast.Param)
        if is_param or name in self.names or self.is_module:
            self.names[name].append(node)
        else:
            self.unscoped_names[name].append(node)

    def make_global(self, names):
        self.global_names.update(names)

    def resolve(self):
        # print 'RESOVE', self.node, self.unscoped_names.keys()

        # Extract global names to module scope
        for name in self.global_names:
            nodes = self.unscoped_names.pop(name, [])
            self.module.names[name].extend(nodes)

        # TODO: add nonlocal support here

        # Detect local names
        for name, nodes in list(self.unscoped_names.items()):
            if self.is_module or any(is_write, nodes):
                self.names[name].extend(nodes)
                self.unscoped_names.pop(name)

    def pass_unscoped(self, other_scope):
        # print "Passing unscoped", self.unscoped_names.keys(), "from", self.node, "to", other_scope.node
        for name, nodes in self.unscoped_names.items():
            if name in other_scope.names:
                other_scope.names[name].extend(nodes)
            else:
                other_scope.unscoped_names[name].extend(nodes)
        self.unscoped_names = empty(self.unscoped_names)

    def walk(self):
        for name, nodes in self.names.items():
            yield self, name, nodes

        for child in self.children:
            for scope, name, nodes in child.walk():
                yield scope, name, nodes

    # Stringification

    def dump(self, indent=''):
        name = self.node.__class__.__name__
        if hasattr(self.node, 'name'):
            name += ' ' + self.node.name
        if hasattr(self.node, 'lineno'):
            name += ' a line %d' % self.node.lineno

        title = indent + 'Scope ' + name
        names = '\n'.join(indent + '  %s = %s' % (name, nodes_str(nodes))
                          for name, nodes in sorted(self.names.items()))
        unscoped = '\n'.join(indent + '  unscoped %s = %s' % (name, nodes_str(nodes))
                             for name, nodes in sorted(self.unscoped_names.items()))
        children = ''.join(c.dump(indent + '  ') for c in self.children)

        return '\n'.join([title, names, unscoped, children])

    def __str__(self):
        return self.dump()


class TreeLinker(ast.NodeVisitor):
    def __init__(self):
        self.stack = deque()

    def visit(self, node):
        if self.stack:
            node.up = self.stack[-1]
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()


class ScopeBuilder(ast.NodeVisitor):
    def __init__(self):
        self.scopes = deque()

    def visit_all(self, *node_lists):
        for node in icat(node_lists):
            self.visit(node)

    # Scope mechanics
    @property
    def scope(self):
        if self.scopes:
            return self.scopes[-1]
        else:
            return None

    def push_scope(self, node):
        self.scopes.append(Scope(self.scope, node))

    def pop_scope(self):
        current = self.scope
        current.resolve()
        self.scopes.pop()
        if self.scope:
            current.pass_unscoped(self.scope)
        return current

    # Visiting
    def visit_Module(self, node):
        self.push_scope(node)
        self.generic_visit(node)
        return self.pop_scope()

    def visit_Import(self, node):
        for alias in node.names:
            self.scope.add(alias.asname or alias.name, node)

    def visit_ImportFrom(self, node):
        if node.module != '__future__':
            self.visit_Import(node)

    def visit_ClassDef(self, node):
        self.scope.add(node.name, node)
        # TODO: handle python 3 style metaclass
        self.visit_all(node.decorator_list, node.bases)

        self.push_scope(node)
        self.visit_all(node.body)
        return self.pop_scope()

    def visit_FunctionDef(self, node):
        # print 'visit_FunctionDef'
        self.scope.add(node.name, node)
        self.visit_all(node.decorator_list, node.args.defaults)

        self.push_scope(node)
        self.visit_all(node.args.args, node.body)
        # Visit vararg and kwarg
        # NOTE: arguments node doesn't have lineno and col_offset,
        #       so we copy them from a function node
        node.args.lineno = node.lineno
        node.args.col_offset = node.col_offset
        if node.args.vararg:
            self.scope.add(node.args.vararg, node.args)
        if node.args.kwarg:
            self.scope.add(node.args.kwarg, node.args)
        # TODO: handle kwonlyargs
        # print 'exit visit_FunctionDef', self.scope.unscoped_names
        return self.pop_scope()

    def visit_Name(self, node):
        # print 'Name', node.id, node.ctx
        # TODO: respect assignments to these or make it a separate error
        if node.id not in BUILTINS:
            self.scope.add(node.id, node)

    def visit_Global(self, node):
        self.scope.make_global(node.names)
