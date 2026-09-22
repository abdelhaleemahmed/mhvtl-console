"""Every call a view makes on a service object matches the method it calls.

Three times in this refactor a view or a service called a method with an
argument it did not take - bulk tape creation, the create-library media step,
the bulk page's keyword names - and each time the TypeError was caught and
turned into a warning, so the tests (which replaced those calls) passed and
the page quietly did nothing.

This reads the view modules without running them. For each name bound to an
instance of a class from apps.libraries.services -
by `x = SomeService()`, by a factory returning one, or `SomeService().m()`
directly - it finds every `x.method(...)` and binds the call's arguments to
the method's real signature. A missing method or arguments that do not bind
fail the test, with the file and line.

Starred arguments (*args, **kwargs) cannot be checked statically and are
skipped. The check covers the classes it can resolve; it is not a type
checker.
"""
import ast
import importlib
import inspect
from pathlib import Path

from django.test import TestCase

APP = Path(__file__).resolve().parents[1]
PACKAGE = 'apps.libraries'

#: The modules whose calls are checked: everything that is not a service,
#: a test or a migration - that is, the callers.
SKIP_PARTS = {'services', 'tests', 'migrations', '__pycache__'}

OWNERS = ('apps.libraries.services',)


def _modules():
    for path in sorted(APP.rglob('*.py')):
        if SKIP_PARTS & set(path.relative_to(APP).parts):
            continue
        yield path


def _service_class(obj):
    """obj if it is a class from services, else None."""
    if inspect.isclass(obj) and obj.__module__.startswith(OWNERS):
        return obj
    return None


def _resolve_import(node: ast.ImportFrom, module_name: str):
    """{local name: object} for one from-import, where it can be imported."""
    if node.level:
        base = module_name.rsplit('.', node.level)[0]
        source = f'{base}.{node.module}' if node.module else base
    else:
        source = node.module
    found = {}
    try:
        module = importlib.import_module(source)
    except Exception:                                  # noqa: BLE001 - optional imports
        return found
    for alias in node.names:
        obj = getattr(module, alias.name, None)
        if obj is not None:
            found[alias.asname or alias.name] = obj
    return found


class _Checker(ast.NodeVisitor):

    def __init__(self, path: Path):
        self.path = path
        self.module_name = PACKAGE + '.' + '.'.join(
            path.relative_to(APP).with_suffix('').parts)
        self.tree = ast.parse(path.read_text())
        self.names = {}          # local name -> imported object
        self.factories = {}      # function name -> class it returns
        self.problems = []
        self.checked = 0

    # -- what names mean ---------------------------------------------------

    def _collect(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                self.names.update(_resolve_import(node, self.module_name))
        for name, obj in list(self.names.items()):
            if inspect.isfunction(obj) and obj.__module__.startswith(OWNERS):
                returned = inspect.signature(obj).return_annotation
                if _service_class(returned):
                    self.factories[name] = returned
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Return) and inner.value is not None:
                        cls = self._class_of(inner.value)
                        if cls:
                            self.factories[node.name] = cls

    def _class_of(self, expr):
        """The service class an expression produces, if it is a known one."""
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
            name = expr.func.id
            if name in self.factories:
                return self.factories[name]
            return _service_class(self.names.get(name))
        return None

    # -- checking ------------------------------------------------------------

    def run(self):
        self._collect()
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._check_function(node)
        return self

    def _check_function(self, function):
        bound = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                cls = self._class_of(node.value)
                if cls:
                    bound[node.targets[0].id] = cls
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            target = node.func.value
            if isinstance(target, ast.Name) and target.id in bound:
                self._check_call(bound[target.id], node)
            elif isinstance(target, ast.Call):
                cls = self._class_of(target)
                if cls:
                    self._check_call(cls, node)

    def _check_call(self, cls, call):
        where = f'{self.path.relative_to(APP)}:{call.lineno}'
        method = call.func.attr
        attribute = inspect.getattr_static(cls, method, None)
        if attribute is None:
            self.problems.append(f'{where}: {cls.__name__} has no {method}()')
            return
        if not callable(getattr(cls, method)) or isinstance(attribute, property):
            return
        if any(isinstance(a, ast.Starred) for a in call.args) \
                or any(k.arg is None for k in call.keywords):
            return
        signature = inspect.signature(getattr(cls, method))
        is_static = isinstance(attribute, (staticmethod, classmethod))
        args = ([] if is_static else [object()]) + [object()] * len(call.args)
        kwargs = {k.arg: object() for k in call.keywords}
        try:
            signature.bind(*args, **kwargs)
        except TypeError as exc:
            self.problems.append(
                f'{where}: {cls.__name__}.{method}{signature} called with '
                f'{len(call.args)} positional and {sorted(kwargs)}: {exc}')
        self.checked += 1


class CallSignatureTests(TestCase):

    def test_every_service_call_binds(self):
        problems, checked = [], 0
        for path in _modules():
            checker = _Checker(path).run()
            problems += checker.problems
            checked += checker.checked
        self.assertGreater(checked, 50, 'the checker resolved too few calls to trust')
        self.assertEqual(problems, [])

    def test_the_checker_catches_a_bad_call(self):
        """The bulk-page bug's shape: a keyword the method does not take."""
        import tempfile
        source = (
            'from apps.libraries.services.tapes import TapeService\n'
            'def view():\n'
            '    service = TapeService()\n'
            '    service.create_bulk(library_id=1, count=2, barcode_prefix="E01")\n'
            '    service.no_such_method()\n')
        with tempfile.TemporaryDirectory(dir=APP) as scratch:
            path = Path(scratch) / 'bad_view.py'
            path.write_text(source)
            checker = _Checker(path).run()
        self.assertEqual(len(checker.problems), 2, checker.problems)
        self.assertIn('barcode_prefix', checker.problems[0])
        self.assertIn('no no_such_method()', checker.problems[1])
