import ast
import clang.cindex
import javalang
from abc import ABC, abstractmethod
import os
import re

try:
    from verilog_ast import VerilogAstExtractor, VerilogAstUnavailable
except Exception:
    try:
        from .verilog_ast import VerilogAstExtractor, VerilogAstUnavailable
    except Exception:
        VerilogAstExtractor = None
        VerilogAstUnavailable = RuntimeError

# === 新增：扩展名 → 语言 的映射 ==============
EXT_LANG_MAP = {
    '.py':   'python',
    '.java': 'java',
    '.cpp':  'cpp', '.cc': 'cpp', '.cxx': 'cpp',
    '.hpp':  'cpp', '.h':  'cpp',
    '.v':    'verilog', '.sv': 'verilog',
    '.vh':   'verilog', '.svh': 'verilog',
}

def language_by_extension(file_path: str) -> str | None:
    """根据文件扩展名推断语言（不支持则返回 None）"""
    for ext, lang in EXT_LANG_MAP.items():
        if file_path.endswith(ext):
            return lang
    return None
# =========================================

class MethodCallVisitor(ast.NodeVisitor):
    def __init__(self, caller_method, all_methods, kg, imports=None):
        self.caller = caller_method
        self.all_methods = all_methods
        self.kg = kg
        self.imports = imports or {}
        self.processed_calls = set()
        
    def visit_Call(self, node):
        try:
            module_path = None
            method_name = None
            
            if isinstance(node.func, ast.Name):
                method_name = node.func.id
                if method_name in self.imports:
                    full_path = self.imports[method_name]
                    if '.' in full_path:
                        module_path, method_name = full_path.rsplit('.', 1)
                    else:
                        module_path = full_path
                    
            elif isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    module_name = node.func.value.id
                    method_name = node.func.attr
                    if module_name in self.imports:
                        module_path = self.imports[module_name]
                elif isinstance(node.func.value, ast.Attribute):
                    parts = []
                    current = node.func
                    while isinstance(current, ast.Attribute):
                        parts.append(current.attr)
                        current = current.value
                    if isinstance(current, ast.Name):
                        base_module = current.id
                        if base_module in self.imports:
                            base_path = self.imports[base_module]
                            parts.pop()
                            parts.insert(0, base_path)
                        else:
                            parts.append(base_module)
                    parts.reverse()
                    module_path = '.'.join(parts[:-1])
                    method_name = parts[-1]
                elif isinstance(node.func.value, ast.Name) and node.func.value.id == 'self':
                    method_name = node.func.attr
                    module_path = '.'.join(self.caller['name'].split('.')[:-2]) # Assuming caller name is like module.class.method
            else:
                return # Not a simple call we can resolve easily

            possible_full_names = []
            if module_path:
                possible_full_names.append(f"{module_path}.{method_name}")
            
            # Consider calls within the same module/class
            if 'name' in self.caller and self.caller['name']:
                caller_parts = self.caller['name'].split('.')
                if len(caller_parts) > 1: # module.method or module.class.method
                    current_module_or_class_path = '.'.join(caller_parts[:-1])
                    possible_full_names.append(f"{current_module_or_class_path}.{method_name}")
                if len(caller_parts) > 2: # module.class.method, consider methods in the same class
                     current_class_path = '.'.join(caller_parts[:-1])
                     possible_full_names.append(f"{current_class_path}.{method_name}")


            # Direct name (e.g. global function in the same file, or built-in)
            possible_full_names.append(method_name)

            for callee in self.all_methods:
                callee_name = callee['name']
                # Check if callee_name matches any of the possible constructed full names
                if any(callee_name == full_name for full_name in possible_full_names) or \
                   (module_path and callee_name.startswith(f"{module_path}.") and callee_name.endswith(f".{method_name}")):
                    
                    call_signature = callee.get('signature', callee_name) # Use signature if available
                    
                    # Avoid processing the same call multiple times for the same caller
                    if (self.caller['name'], callee_name) in self.processed_calls:
                        break 
                    self.processed_calls.add((self.caller['name'], callee_name))

                    # Ensure caller is created as an entity (if not already)
                    # self.kg.create_method_entity(
                    #     self.caller['name'],
                    #     self.caller.get('signature', self.caller['name']),
                    #     self.caller['file_path'],
                    #     self.caller['start_line'],
                    #     self.caller['end_line'],
                    #     self.caller.get('source_code', ''), # Make sure source_code is available
                    #     self.caller.get('doc_string', ''),  # Make sure doc_string is available
                    #     STRONG_CONNECTION # This constant needs to be defined or imported
                    # )
                    
                    print(f"Found method call: {self.caller['name']} -> {callee_name}")
                    self.kg.link_method_calls(
                        self.caller['name'],
                        self.caller.get('signature', self.caller['name']),
                        callee_name,
                        call_signature,
                    )
                    break 
        except Exception as e:
            # import traceback
            # print(f"Error while processing method call: {e}\n{traceback.format_exc()}")
            print(f"Error while processing method call for {self.caller.get('name', 'Unknown Caller')} -> {method_name if 'method_name' in locals() else 'Unknown Callee'}: {e}")
        self.generic_visit(node)


class LanguageConfig(ABC):
    def __init__(self, language_name: str):
        self.language = language_name
        self.config = self._load_config()

    @abstractmethod
    def get_comment_prefix(self) -> str:
        pass

    @abstractmethod
    def get_search_patterns(self, entity_name: str) -> dict[str, str]:
        """
        Returns a dictionary of regex patterns for searching entities by name.
        Keys might be 'class', 'method', 'variable', 'import', 'string', etc.
        Values are regex pattern strings.
        """
        pass

    @abstractmethod
    def resolve_qualified_name_to_file_paths(self, base_path: str, qualified_name_parts: list[str]) -> list[tuple[str, str]]:
        """
        Resolves a qualified name (e.g., ['com', 'example', 'MyClass']) to potential file paths.
        Returns a list of (type, path) tuples, where type can be 'file', 'package', etc.
        """
        pass

    def _load_config(self):
        """Return minimal config dict for file extension handling etc."""
        default_configs = {
            'python': {
                'file_extensions': ['.py'],
                'test_file_pattern': 'test_'
            },
            'java': {
                'file_extensions': ['.java'],
                'test_file_pattern': 'Test.java'
            },
            'cpp': {
                'file_extensions': ['.cpp', '.cc', '.cxx', '.hpp', '.h', '.hxx'],
                'test_file_pattern': 'test'
            },
            'verilog': {
                'file_extensions': ['.v', '.sv', '.vh', '.svh'],
                'test_file_pattern': 'tb_'
            }
        }
        return default_configs.get(self.language, {'file_extensions': [], 'test_file_pattern': ''})

class PythonLanguageConfig(LanguageConfig):
    def __init__(self):
        super().__init__('python')

    def get_comment_prefix(self) -> str:
        return "#"

    def get_search_patterns(self, entity_name: str) -> dict[str, str]:
        escaped_name = re.escape(entity_name)
        return {
            'class': rf'class\s+{escaped_name}\(',
            'method': rf'def\s+{escaped_name}\(',
            'global_var': rf'^{escaped_name}\s*=',  # Module-level variable
            'instance_var': rf'self\.{escaped_name}\s*=', # Inside class methods
            'local_var': rf'^\s*{escaped_name}\s*=', # Inside functions/methods, simple assignment
            'import_from': rf'from\s+[\w.]+\s+import\s+.*{escaped_name}',
            'import_module': rf'import\s+[\w.]*?{escaped_name}[\w.]*?',
            'string': rf'([\'"]){escaped_name}\\1',
            'comment': rf'#.*{escaped_name}',
            'decorator': rf'@{escaped_name}',
        }

    def resolve_qualified_name_to_file_paths(self, base_path: str, qualified_name_parts: list[str]) -> list[tuple[str, str]]:
        paths = []
        # Module: a.b.c -> a/b/c.py
        paths.append(('file', os.path.join(base_path, *qualified_name_parts) + '.py'))
        # Package: a.b.c -> a/b/c/__init__.py
        paths.append(('file', os.path.join(base_path, *qualified_name_parts, '__init__.py')))
        # Directory (package itself): a.b.c -> a/b/c/
        paths.append(('package', os.path.join(base_path, *qualified_name_parts)))
        return paths

class JavaLanguageConfig(LanguageConfig):
    def __init__(self):
        super().__init__('java')

    def get_comment_prefix(self) -> str:
        return "//"

    def get_search_patterns(self, entity_name: str) -> dict[str, str]:
        escaped_name = re.escape(entity_name)
        # Basic patterns, can be significantly improved for accuracy
        return {
            'class': rf'class\s+{escaped_name}\s*{{',
            'interface': rf'interface\s+{escaped_name}\s*{{',
            'method': rf'(?:public|protected|private|static|final|synchronized|abstract|default|\s)*\s*[\w.<>,\\[\\]?]+\s+{escaped_name}\s*\([^)]*\)\s*(?:{{|throws|;)',
            'variable_declaration': rf'(?:private|public|protected|static|final)?\s*[\w.<>,\\[\\]]+\s+{escaped_name}\s*(?:=|;)',
            'import': rf'import\s+(?:static\s+)?(?:[\w.]+\.)?{escaped_name}(?:\.\*)?;',
            'string': rf'"[^"]*{escaped_name}[^"]*"',
            'comment': rf'(?://.*{escaped_name}|/\*.*?{escaped_name}.*?\*/)',
            'annotation': rf'@{escaped_name}',
        }

    def resolve_qualified_name_to_file_paths(self, base_path: str, qualified_name_parts: list[str]) -> list[tuple[str, str]]:
        paths = []
        # Class: com.example.MyClass -> com/example/MyClass.java
        if qualified_name_parts:
            paths.append(('file', os.path.join(base_path, *qualified_name_parts) + '.java'))
        # Package: com.example -> com/example/ (as a directory)
        paths.append(('package', os.path.join(base_path, *qualified_name_parts)))
        return paths

class CppLanguageConfig(LanguageConfig):
    def __init__(self):
        super().__init__('cpp')

    def get_comment_prefix(self) -> str:
        return "//" # or /* for block comments, but // is simpler for single line prefix

    def get_search_patterns(self, entity_name: str) -> dict[str, str]:
        # Basic C++ patterns, needs significant improvement for real-world accuracy
        escaped_name = re.escape(entity_name)
        return {
            'class_struct_union': rf'(?:class|struct|union)\s+{escaped_name}\s*{{',
            'function_method': rf'[\w:]+\s+{escaped_name}\s*\([^)]*\)\s*{{', # Very basic
            'variable_declaration': rf'[\w:]+\s+{escaped_name}\s*(?:=|;|\\[|\()', # Very basic
            'namespace': rf'namespace\s+{escaped_name}\s*{{',
            'include': rf'#include\s*(?:<[^>]*{escaped_name}[^>]*>|"[^"]*{escaped_name}[^"]*")',
            'define': rf'#define\s+{escaped_name}',
            'string': rf'"[^"]*{escaped_name}[^"]*"',
            'comment': rf'(?://.*{escaped_name}|/\*.*?{escaped_name}.*?\*/)',
        }

    def resolve_qualified_name_to_file_paths(self, base_path: str, qualified_name_parts: list[str]) -> list[tuple[str, str]]:
        paths = []
        # Header file for a class/entity (common convention)
        if qualified_name_parts:
            paths.append(('file', os.path.join(base_path, *qualified_name_parts) + '.h'))
            paths.append(('file', os.path.join(base_path, *qualified_name_parts) + '.hpp'))
            paths.append(('file', os.path.join(base_path, *qualified_name_parts) + '.hxx'))
        # Source file (common convention)
        if qualified_name_parts:
            paths.append(('file', os.path.join(base_path, *qualified_name_parts) + '.cpp'))
            paths.append(('file', os.path.join(base_path, *qualified_name_parts) + '.cxx'))
            paths.append(('file', os.path.join(base_path, *qualified_name_parts) + '.cc'))
        # Directory (for namespaces or broader components)
        paths.append(('package', os.path.join(base_path, *qualified_name_parts))) # 'package' type is generic here
        return paths

class VerilogLanguageConfig(LanguageConfig):
    def __init__(self):
        super().__init__('verilog')

    def get_comment_prefix(self) -> str:
        return "//"

    def get_search_patterns(self, entity_name: str) -> dict[str, str]:
        escaped_name = re.escape(entity_name)
        return {
            'module': rf'\b(?:module|interface|package|program)\s+{escaped_name}\b',
            'function': rf'\bfunction\b[\s\S]*?\b{escaped_name}\b',
            'task': rf'\btask\b[\s\S]*?\b{escaped_name}\b',
            'procedural_block': rf'\b(?:always|always_ff|always_comb|always_latch|initial)\b[\s\S]*?\b{escaped_name}\b',
            'continuous_assign': rf'\bassign\s+{escaped_name}\b',
            'instance': rf'\b{escaped_name}\s*(?:#\s*\([^;]*?\)\s*)?[A-Za-z_][\w$]*\s*\(',
            'macro': rf'`(?:define|ifdef|ifndef|elsif)\s+{escaped_name}\b',
            'signal_declaration': rf'\b(?:wire|reg|logic|input|output|inout|parameter|localparam)\b[^;]*\b{escaped_name}\b',
            'include': rf'`include\s+"[^"]*{escaped_name}[^"]*"',
            'comment': rf'(?://.*{escaped_name}|/\*[\s\S]*?{escaped_name}[\s\S]*?\*/)',
        }

    def resolve_qualified_name_to_file_paths(self, base_path: str, qualified_name_parts: list[str]) -> list[tuple[str, str]]:
        paths = []
        if not qualified_name_parts:
            return paths

        search_roots = ['', 'src', 'rtl', os.path.join('src', 'rtl'), 'include', os.path.join('src', 'include'), 'tb', 'test', 'tests']
        possible_file_ref = '/'.join(qualified_name_parts)
        has_file_extension = len(qualified_name_parts) > 1 and qualified_name_parts[-1] in {'v', 'sv', 'vh', 'svh'}
        if has_file_extension:
            possible_file_ref = '/'.join(qualified_name_parts[:-1]) + '.' + qualified_name_parts[-1]
            for root in search_roots:
                paths.append(('file', os.path.join(base_path, root, possible_file_ref)))

        stem = os.path.splitext(os.path.basename(possible_file_ref))[0] if has_file_extension else qualified_name_parts[-1]
        if '/' in stem or '\\' in stem:
            paths.append(('file', os.path.join(base_path, stem)))

        if not has_file_extension:
            for ext in self.config['file_extensions']:
                for root in search_roots:
                    paths.append(('file', os.path.join(base_path, root, possible_file_ref + ext)))
                    paths.append(('file', os.path.join(base_path, root, stem + ext)))

        paths.append(('package', os.path.join(base_path, possible_file_ref)))
        return paths

class LanguageConfigFactory:
    """Factory to create concrete LanguageConfig instances based on language name."""
    @staticmethod
    def get_config(language: str) -> LanguageConfig:
        lang = language.lower()
        if lang == 'python':
            return PythonLanguageConfig()
        elif lang == 'java':
            return JavaLanguageConfig()
        elif lang == 'cpp':
            return CppLanguageConfig()
        elif lang == 'verilog':
            return VerilogLanguageConfig()
        else:
            raise ValueError(f"Unsupported language for LanguageConfigFactory: {language}")

class BaseParser(ABC):
    def __init__(self, language_name: str):
        self.language_config = LanguageConfigFactory.get_config(language_name)
        self._file_ast_cache = {} # Cache for parsed file ASTs

    @abstractmethod
    def get_compilation_unit(self, file_path: str):
        """Parses the file and returns the root AST node (e.g., CompilationUnit for Java).
           Results should be cached to avoid re-parsing.
        """
        pass

    @abstractmethod
    def parse_file(self, file_path):
        pass
        
    @abstractmethod
    def extract_classes(self, file_path):
        pass
        
    @abstractmethod
    def extract_methods(self, file_path):
        pass

    @abstractmethod
    def get_imports(self, file_path):
        pass

    @abstractmethod
    def get_global_methods(self, file_path, repo_name):
        pass

    @abstractmethod
    def get_global_variables(self, file_path, repo_name):
        pass

    @abstractmethod
    def analyze_method_calls_in_method(self, local_method_info, all_methods, kg, imports, repo_name):
        pass

    @abstractmethod
    def analyze_snippet_for_references(self, code_snippet_string):
        pass

class PythonParser(BaseParser):
    def __init__(self):
        super().__init__('python')

    def _clean_path(self, file_path: str) -> str:
        """Removes 'playground/' prefix and the project directory from a path."""
        rel_path = os.path.relpath(file_path)
        prefix = 'playground' + os.sep
        if rel_path.startswith(prefix):
            path_after_playground = rel_path[len(prefix):]
            parts = path_after_playground.split(os.sep)
            if len(parts) > 1:
                return os.sep.join(parts[1:])
            else:
                return path_after_playground
        return rel_path

    def get_compilation_unit(self, file_path: str):
        if file_path in self._file_ast_cache:
            return self._file_ast_cache[file_path]
        try:
            content = self._read_file(file_path)
            if content is None:
                return None
            tree = ast.parse(content)
            self._file_ast_cache[file_path] = tree
            return tree
        except Exception as e:
            print(f"Error parsing Python file {file_path} for AST: {e}")
            self._file_ast_cache[file_path] = None # Cache None on error
            return None

    def parse_file(self, file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return ast.parse(content), content
            
    def extract_classes(self, file_path):
        tree, content = self.parse_file(file_path)
        clean_file_path = self._clean_path(file_path)
        module_path = clean_file_path.replace(os.sep, '.').replace('.py', '')
        classes = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                qualified_class_name = f"{module_path}.{node.name}"
                classes.append({
                    'name': qualified_class_name,
                    'file_path': clean_file_path,
                    'start_line': node.lineno,
                    'end_line': node.end_lineno,
                    'source_code': ast.get_source_segment(content, node) if hasattr(ast, 'get_source_segment') else ast.unparse(node),
                    'doc_string': ast.get_docstring(node) or '',
                    'methods': self._extract_class_methods(node, content, qualified_class_name, clean_file_path)
                })
        return classes
        
    def _extract_class_methods(self, class_node, content, qualified_class_name, clean_file_path):
        methods = []
        for node in class_node.body:
            if isinstance(node, ast.FunctionDef):
                params = [a.arg for a in node.args.args]
                method_signature = f"{qualified_class_name}.{node.name}({', '.join(params)})"
                methods.append({
                    'name': f"{qualified_class_name}.{node.name}",
                    'signature': method_signature,
                    'file_path': clean_file_path,
                    'start_line': node.lineno,
                    'end_line': node.end_lineno,
                    'source_code': ast.get_source_segment(content, node) if hasattr(ast, 'get_source_segment') else ast.unparse(node),
                    'doc_string': ast.get_docstring(node)
                })
        return methods
        
    def extract_methods(self, file_path):
        tree, content = self.parse_file(file_path)
        clean_file_path = self._clean_path(file_path)
        module_path = clean_file_path.replace(os.sep, '.').replace('.py', '')
        methods = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                params = [a.arg for a in node.args.args]
                qualified_name = f"{module_path}.{node.name}"
                method_signature = f"{qualified_name}({', '.join(params)})"
                methods.append({
                    'name': qualified_name,
                    'signature': method_signature,
                    'file_path': clean_file_path,
                    'start_line': node.lineno,
                    'end_line': node.end_lineno,
                    'source_code': ast.get_source_segment(content, node) if hasattr(ast, 'get_source_segment') else ast.unparse(node),
                    'doc_string': ast.get_docstring(node)
                })
        return methods

    def get_imports(self, file_path):
        imports = {}
        try:
            content = self._read_file(file_path)
            tree = ast.parse(content)
                
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports[alias.asname or alias.name] = alias.name
                        
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ''
                    for alias in node.names:
                        if alias.asname:
                            imports[alias.asname] = f"{module}.{alias.name}"
                        else:
                            imports[alias.name] = f"{module}.{alias.name}"
                            
            return imports
            
        except Exception as e:
            print(f"Error while parsing import statements in file {file_path}: {str(e)}")
            return {}

    def get_global_methods(self, file_path, repo_name):
        content = self._read_file(file_path)
        tree = ast.parse(content)
        
        clean_file_path = self._clean_path(file_path)
        module_path = clean_file_path.replace(os.sep, '.').replace('.py', '')

        methods = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                method_name = node.name
                params = [a.arg for a in node.args.args]
                method_signature = f"{module_path}.{method_name}({', '.join(params)})"
                doc_string = ast.get_docstring(node) or ''
                methods.append({
                    "name": f"{module_path}.{method_name}",
                    "signature": method_signature,
                    'file_path': clean_file_path,
                    "start_line": node.lineno,
                    "source_code": ast.get_source_segment(content, node),
                    "end_line": node.end_lineno if hasattr(node, 'end_lineno') else None,
                    "doc_string": doc_string,
                })
        return methods

    def get_global_variables(self, file_path, repo_name):
        content = self._read_file(file_path)
        tree = ast.parse(content)

        clean_file_path = self._clean_path(file_path)
        module_path = clean_file_path.replace(os.sep, '.').replace('.py', '')

        variables = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        try:
                            value = ast.literal_eval(node.value)
                        except (ValueError, SyntaxError):
                            value = ast.get_source_segment(content, node.value)
                        variables.append({
                            "name": f"{module_path}.{target.id}",
                            "signature": f"{module_path}.{target.id} = {value}",
                            "file_path": clean_file_path,
                            "start_line": node.lineno,
                            "end_line": node.end_lineno if hasattr(node, 'end_lineno') else None,
                            "source_code": ast.get_source_segment(content, node),
                            "doc_string": "",
                        })
        return variables

    def _read_file(self, file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def analyze_method_calls_in_method(self, local_method_info, all_methods, kg, imports, repo_name):
        if 'source_code' not in local_method_info or not local_method_info['source_code']:
            # print(f"Skipping method call analysis for {local_method_info.get('name')} due to missing source code.")
            return
        try:
            tree = ast.parse(local_method_info['source_code'])
            visitor = MethodCallVisitor(
                caller_method=local_method_info,
                all_methods=all_methods,
                kg=kg,
                imports=imports,
            )
            visitor.visit(tree)
        except SyntaxError as e:
            print(f"SyntaxError parsing method {local_method_info.get('name', 'unknown method')} in {local_method_info.get('file_path', 'unknown file')}: {e}")
        except Exception as e:
            # import traceback
            # print(f"Error analyzing method calls for {local_method_info.get('name', 'unknown method')}: {e}\n{traceback.format_exc()}")
            print(f"Error analyzing method calls for {local_method_info.get('name', 'unknown method')}: {e}")

    def analyze_snippet_for_references(self, code_snippet_string):
        class MethodCallCollector(ast.NodeVisitor):
            def __init__(self):
                self.calls = []
            
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        alias = node.func.value.id
                        method = node.func.attr
                        self.calls.append(('call', alias, method))
                
                # This part seems specific and might need review for general snippets
                # For example, `operator` and `&` might not be universally what we want to extract.
                # Keeping it for now to match original logic.
                elif isinstance(node.func, ast.BinOp): # Original code had BinOp check here
                    # Original code added ('operator', 'operator', '&').
                    # This seems very specific. If it's about identifying general operator usage, 
                    # it needs a different approach. If it is specific to a known pattern, it's okay.
                    # For now, I'll comment it out as it's unlikely to be a general reference type.
                    # self.calls.append(('operator', 'operator', '&')) 
                    pass
                    
                self.generic_visit(node)
        
        references = set()
        try:
            imports = {}
            tree = ast.parse(code_snippet_string)
            
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module
                    for name_node in node.names: # Corrected to name_node
                        if name_node.asname:
                            imports[name_node.asname] = f"{module}.{name_node.name}"
                        else:
                            imports[name_node.name] = f"{module}.{name_node.name}"
                elif isinstance(node, ast.Import): # Handling simple imports too
                    for alias in node.names:
                        imports[alias.asname or alias.name] = alias.name

            collector = MethodCallCollector()
            collector.visit(tree)
            
            for alias, full_path in imports.items():
                references.add(('import', full_path))
            
            for call_type, alias, method in collector.calls:
                # if call_type == 'operator': # See comment in MethodCallCollector
                #     references.add(('import', f'{alias}.{method}'))
                # else: 
                if alias in imports: # Only if the base object of the call is an identified import
                    references.add(('call', f"{imports[alias]}.{method}"))
                # else: # Potentially a call to a global/built-in or method in the same snippet
                      # This part was not explicitly handled in the original snippet analyzer 
                      # for direct calls without a known imported base, so keeping it simple.
                      # references.add(('call', f".{method}")) # Example: call to a local func

            return sorted(list(references))
            
        except SyntaxError: # If snippet is not valid Python
            return []
        except Exception as e:
            print(f"Error while analyzing Python code snippet: {e}")
            return []

class CppParser(BaseParser):
    def __init__(self):
        super().__init__('cpp')
        # Initialize Clang index if not already done by a shared instance
        try:
            if not clang.cindex.Config.loaded:
                # Attempt to find libclang.so or libclang.dylib
                # Common paths, adjust if necessary for your system
                libclang_paths = [
                    '/usr/lib/llvm-14/lib/libclang-14.so.1', # Example for specific LLVM version
                    '/usr/lib/x86_64-linux-gnu/libclang-14.so.1',
                    '/usr/lib/libclang.so',
                    '/usr/local/lib/libclang.so',
                    '/Library/Developer/CommandLineTools/usr/lib/libclang.dylib', # macOS
                ]
                found_path = None
                for path_option in libclang_paths:
                    if os.path.exists(path_option):
                        clang.cindex.Config.set_library_file(path_option)
                        found_path = path_option
                        break
                if not found_path:
                    print("Warning: libclang not found at specified paths. C++ parsing might fail.")
            self.index = clang.cindex.Index.create()
        except Exception as e:
            print(f"Error initializing libclang: {e}. C++ parsing will be unavailable.")
            self.index = None

    def get_compilation_unit(self, file_path: str):
        if not self.index:
            return None
        if file_path in self._file_ast_cache:
            return self._file_ast_cache[file_path]
        try:
            # For C++, TU (Translation Unit) is the equivalent of CompilationUnit
            # Parsing options can be added here, e.g., include directories
            tu = self.index.parse(file_path, args=['-std=c++17']) # Example args
            if not tu:
                print(f"Failed to parse C++ file {file_path}")
                self._file_ast_cache[file_path] = None
                return None
            self._file_ast_cache[file_path] = tu
            return tu
        except clang.cindex.TranslationUnitLoadError as e:
            print(f"Clang TranslationUnitLoadError for {file_path}: {e}")
            self._file_ast_cache[file_path] = None
            return None
        except Exception as e:
            print(f"Error parsing C++ file {file_path} with Clang: {e}")
            self._file_ast_cache[file_path] = None
            return None

    def parse_file(self, file_path):
        index = clang.cindex.Index.create()
        return index.parse(file_path)
        
    def extract_classes(self, file_path):
        tu = self.parse_file(file_path)
        classes = []
        for node in tu.cursor.walk_preorder():
            if node.kind == clang.cindex.CursorKind.CLASS_DECL:
                classes.append({
                    'name': node.spelling,
                    'start_line': node.location.line,
                    'end_line': node.extent.end.line,
                    'methods': self._extract_class_methods(node)
                })
        return classes
        
    def _extract_class_methods(self, class_node):
        methods = []
        for node in class_node.get_children():
            if node.kind == clang.cindex.CursorKind.CXX_METHOD:
                methods.append({
                    'name': node.spelling,
                    'signature': node.displayname,
                    'start_line': node.location.line,
                    'end_line': node.extent.end.line,
                    'source_code': self._get_source_code(node),
                    'doc_string': self._get_docstring(node)
                })
        return methods
        
    def _get_source_code(self, node):
        start = node.extent.start.offset
        end = node.extent.end.offset
        with open(node.location.file.name, 'r', encoding='utf-8') as f:
            return f.read()[start:end]
            
    def _get_docstring(self, node):
        for child in node.get_children():
            if child.kind == clang.cindex.CursorKind.COMMENT:
                return child.spelling
        return None
        
    def extract_methods(self, file_path):
        tu = self.parse_file(file_path)
        methods = []
        for node in tu.cursor.walk_preorder():
            if node.kind == clang.cindex.CursorKind.FUNCTION_DECL:
                methods.append({
                    'name': node.spelling,
                    'signature': node.displayname,
                    'start_line': node.location.line,
                    'end_line': node.extent.end.line,
                    'source_code': self._get_source_code(node),
                    'doc_string': self._get_docstring(node)
                })
        return methods

    def get_imports(self, file_path):
        imports = {}
        try:
            tu = self.parse_file(file_path)
            for node in tu.cursor.walk_preorder():
                if node.kind == clang.cindex.CursorKind.INCLUSION_DIRECTIVE:
                    imports[node.spelling] = node.spelling
            return imports
        except Exception as e:
            print(f"Error while parsing include statements in file {file_path}: {str(e)}")
            return {}

    def get_global_methods(self, file_path, repo_name):
        tu = self.parse_file(file_path)
        methods = []
        for node in tu.cursor.walk_preorder():
            if node.kind == clang.cindex.CursorKind.FUNCTION_DECL:
                methods.append({
                    'name': node.spelling,
                    'signature': node.displayname,
                    'start_line': node.location.line,
                    'end_line': node.extent.end.line,
                    'source_code': self._get_source_code(node),
                    'doc_string': self._get_docstring(node)
                })
        return methods

    def get_global_variables(self, file_path, repo_name):
        tu = self.parse_file(file_path)
        variables = []
        for node in tu.cursor.walk_preorder():
            if node.kind == clang.cindex.CursorKind.VAR_DECL:
                variables.append({
                    'name': node.spelling,
                    'signature': node.displayname,
                    'start_line': node.location.line,
                    'end_line': node.extent.end.line,
                    'source_code': self._get_source_code(node),
                    'doc_string': self._get_docstring(node)
                })
        return variables

    def analyze_method_calls_in_method(self, local_method_info, all_methods, kg, imports, repo_name):
        # print(f"Method call analysis not implemented for C++ for method {local_method_info.get('name')}")
        pass

    def analyze_snippet_for_references(self, code_snippet_string):
        # Placeholder: C++ snippet analysis would require a different approach (e.g., regex or temp compilation)
        return []

class JavaParser(BaseParser):
    """Java 源码解析器，使用 javalang 解析。返回 AST 以及源码文本"""

    def __init__(self):
        super().__init__('java')

    def _attach_parents(self, node, parent=None):
        """
        Recursively attaches a 'parent' attribute to each node in the AST.
        """
        if node is None:
            return

        # For javalang, nodes are either javalang.ast.Node instances or lists/tuples of them.
        # Primitive types (str, int, bool) or None don't need parent attributes.
        
        if isinstance(node, javalang.tree.Node): # Check if it's a javalang AST Node
            setattr(node, 'parent', parent)
            
            # Iterate over attributes that might contain child nodes or lists of child nodes
            # Common attributes in javalang nodes: 'annotations', 'body', 'declarations', 
            # 'expression', 'arguments', 'parameters', 'type', 'selectors', 'sub_type', etc.
            # A more robust way is to check javalang.tree.Node.children if available,
            # or iterate through __slots__ or fields if defined.
            # javalang nodes store children in specific named attributes.
            # We can inspect common ones or those that are lists/tuples or other Nodes.
            
            # javalang nodes define their children in a 'children' property
            if hasattr(node, 'children') and isinstance(node.children, (list, tuple)):
                for child_or_children_list in node.children:
                    if isinstance(child_or_children_list, (list, tuple)):
                        for child in child_or_children_list:
                            self._attach_parents(child, node)
                    elif isinstance(child_or_children_list, javalang.tree.Node):
                        self._attach_parents(child_or_children_list, node)
            # Some nodes might have children not directly in 'children' attribute,
            # e.g. 'type', 'expressionl', 'expressionr'.
            # This part might need refinement based on javalang's specific AST structure
            # for all node types if the .children attribute isn't comprehensive.
            # However, javalang's design usually makes .children quite reliable.

        elif isinstance(node, (list, tuple)):
            for item in node:
                self._attach_parents(item, parent) # Pass the same parent for items in a list


    def get_compilation_unit(self, file_path: str):
        if file_path in self._file_ast_cache:
            tree = self._file_ast_cache[file_path]
            # Ensure parent attributes are attached if loaded from cache and not already done
            # This check might be redundant if we ensure it's always done before caching,
            # but can be a safety measure.
            if tree and not hasattr(tree, 'parent_attached_marker'): # Add a marker
                self._attach_parents(tree)
                if tree: # Check if tree is not None after trying to attach parents
                     setattr(tree, 'parent_attached_marker', True)
            return tree
        try:
            content = self._read_file(file_path)
            if content is None:
                self._file_ast_cache[file_path] = None
                return None
            tree = javalang.parse.parse(content)
            if tree: # If parsing was successful
                self._attach_parents(tree)
                setattr(tree, 'parent_attached_marker', True) # Mark as processed
            self._file_ast_cache[file_path] = tree
            return tree
        except Exception as e:
            print(f"Error parsing Java file {file_path} for CompilationUnit: {e}")
            self._file_ast_cache[file_path] = None # Cache None on error
            return None

    def _read_file(self, file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def parse_file(self, file_path):
        content = self._read_file(file_path)
        tree = self.get_compilation_unit(file_path)
        return tree, content

    def _find_block_end(self, start_line, content):
        """简单地根据花括号匹配，估算代码块结束行。"""
        lines = content.splitlines()
        brace_level = 0
        # Ensure start_line is valid
        if not (0 < start_line <= len(lines)):
            return start_line # Or handle error appropriately

        for idx in range(start_line - 1, len(lines)):
            line_text = lines[idx]
            # Simple count, may not be accurate with comments or string literals containing braces
            brace_level += line_text.count('{')
            brace_level -= line_text.count('}')
            # If brace_level becomes 0 after the start_line and it's not due to an empty block on the same line
            if brace_level == 0 and idx >= start_line -1 : # Allow block to end on the same line for simple cases
                 # Check if this line actually contained the start of the block
                is_block_start_line = '{' in lines[start_line-1]
                if is_block_start_line and idx == start_line -1 and lines[start_line-1].count('{') == lines[start_line-1].count('}'): # e.g. foo() {}
                    pass # Ends on same line is ok
                elif idx < start_line -1 + (1 if is_block_start_line else 0): # Should not end before it starts or on the start line if multi-line
                    continue

                return idx + 1
        return len(lines) # Fallback: end of file if block seems unclosed

    def extract_classes(self, file_path):
        tree, content = self.parse_file(file_path)
        if not tree: # If parsing failed
            return []
        classes = []
        for _, node in tree.filter(javalang.tree.ClassDeclaration):
            start_line = node.position.line if node.position else -1
            if start_line == -1: continue

            end_line = self._find_block_end(start_line, content)
            classes.append({
                'name': node.name,
                'file_path': file_path, # Add file_path to class info
                'start_line': start_line,
                'end_line': end_line,
                'source_code': '\n'.join(content.splitlines()[start_line-1:end_line]),
                'doc_string': self._get_docstring(node) or '',
                'methods': self._extract_class_methods(node, content, file_path) # Pass file_path
            })
        return classes
        
    def _extract_class_methods(self, class_node, content, file_path_for_methods): # Added file_path_for_methods
        methods = []
        if not class_node.body: # Class body can be None for interfaces sometimes, or empty classes
            return methods

        for member in class_node.body: # Iterate all members of the class body
            if isinstance(member, javalang.tree.MethodDeclaration):
                start_line = member.position.line if member.position else -1
                if start_line == -1: continue

                end_line = self._find_block_end(start_line, content)
                methods.append({
                    'name': member.name,
                    'signature': self._get_method_signature(member, file_path_for_methods), # Pass file_path_for_methods
                    'file_path': file_path_for_methods, # Use passed file_path
                    'start_line': start_line,
                    'end_line': end_line,
                    'source_code': '\n'.join(content.splitlines()[start_line-1:end_line]),
                    'doc_string': self._get_docstring(member)
                })
            elif isinstance(member, javalang.tree.ConstructorDeclaration):
                start_line = member.position.line if member.position else -1
                if start_line == -1: continue

                end_line = self._find_block_end(start_line, content)
                constructor_name_for_dict = class_node.name
                
                package_name_str = ""
                qualified_class_name_str = "" 

                class_name_parts = []
                temp_node = class_node 

                while temp_node:
                    if isinstance(temp_node, (javalang.tree.ClassDeclaration, 
                                              javalang.tree.InterfaceDeclaration, 
                                              javalang.tree.EnumDeclaration)):
                        class_name_parts.insert(0, temp_node.name) 
                        if hasattr(temp_node, 'parent'):
                            temp_node = temp_node.parent
                        else:
                            break
                    elif isinstance(temp_node, javalang.tree.CompilationUnit):
                        # This case might not be strictly necessary for class name parts if CU is always top
                        break
                    else:
                        break # Stop if not a class, interface, enum or compilation unit
                
                if class_name_parts:
                    qualified_class_name_str = ".".join(class_name_parts) 

                # Get package name directly from file_path_for_methods
                if file_path_for_methods:
                    _raw_pkg_name = self._get_package_from_file(file_path_for_methods)
                    if _raw_pkg_name:
                        package_name_str = _raw_pkg_name + "."

                full_constructor_prefix = package_name_str + qualified_class_name_str 

                params_with_names = []
                if member.parameters: 
                    for param in member.parameters:
                        param_type_name = self._get_type_name(param.type)
                        param_name = param.name 
                        params_with_names.append(f"{param_type_name} {param_name}".strip())

                methods.append({
                    'name': constructor_name_for_dict, 
                    'signature': f"{full_constructor_prefix}({', '.join(params_with_names)})",
                    'file_path': file_path_for_methods, 
                    'start_line': start_line,
                    'end_line': end_line,
                    'source_code': '\n'.join(content.splitlines()[start_line-1:end_line]),
                    'doc_string': self._get_docstring(member)
                })
        return methods
        
    def _get_package_from_file(self, file_path):
        """从文件内容中提取包声明。返回包名（不包含分号）或空字符串。"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('package '):
                        # 移除 'package ' 前缀和结尾的分号
                        return line[8:].rstrip(';')
            return ""
        except Exception as e:
            print(f"Error reading package declaration from {file_path}: {e}")
            return ""

    def _get_method_signature(self, method_node, file_path_param: str): # For MethodDeclaration, added file_path_param
        # 从文件内容中获取包名
        package_name_str = ""
        if file_path_param:
            package_name_str = self._get_package_from_file(file_path_param)
            if package_name_str:
                package_name_str += "."

        # 获取类名
        class_name_parts = []
        try:
            current_node = method_node.parent if hasattr(method_node, 'parent') else None
            while current_node:
                if isinstance(current_node, (javalang.tree.ClassDeclaration, 
                                          javalang.tree.InterfaceDeclaration, 
                                          javalang.tree.EnumDeclaration)):
                    class_name_parts.insert(0, current_node.name)
                    if hasattr(current_node, 'parent'):
                        current_node = current_node.parent
                    else:
                        break
                # Stop if current_node is CompilationUnit or something else not a class container
                elif isinstance(current_node, javalang.tree.CompilationUnit):
                    break
                else:
                    break
        except Exception as e:
            print(f"Warning: Error getting class name parts: {e}")

        qualified_class_name_str = ".".join(class_name_parts) + "." if class_name_parts else ""
        
        # 获取返回类型
        return_type = self._get_type_name(method_node.return_type) if method_node.return_type else "void"
        
        # 获取参数列表
        params_with_names = []
        if method_node.parameters:
            for param in method_node.parameters:
                param_type_name = self._get_type_name(param.type)
                param_name = param.name
                params_with_names.append(f"{param_type_name} {param_name}".strip())

        # 构建完整签名：包路径.类名.方法名(参数类型 参数名列表): 返回类型
        return f"{package_name_str}{qualified_class_name_str}{method_node.name}({', '.join(params_with_names)}): {return_type}"

    def _get_type_name(self, type_node):
        if not type_node: return "void"

        base_type_name = type_node.name
        
        # Build qualified name if qualifier exists
        # javalang structure for qualified types like `p.A.B` for a type `B` is
        # Type(name='B', qualifier='p.A')
        if hasattr(type_node, 'qualifier') and type_node.qualifier:
            # Ensure qualifier is a string, not another node structure in some edge cases
            qualifier_str = type_node.qualifier
            if not isinstance(qualifier_str, str): # If qualifier is a ReferenceType itself
                # This case needs careful handling if javalang nests ReferenceType in qualifier
                # For now, assume simple string qualifier or direct name.
                # A more robust solution might involve recursively building up the qualifier.
                # This simplified version handles common cases.
                 pass # Stick with type_node.name if qualifier is complex object

            base_type_name = f"{type_node.qualifier}.{base_type_name}"


        dimensions_str = ""
        if type_node.dimensions: # This is a list of '[]' or similar indications
            dimensions_str = "[]" * len(type_node.dimensions)

        type_args_str = ""
        if hasattr(type_node, 'type_arguments') and type_node.type_arguments:
            # Filter out None arguments that can appear for unbounded wildcards like List<?>
            args = [self._get_type_name(arg) for arg in type_node.type_arguments if arg is not None]
            if args: # Only add <> if there are actual type arguments
                type_args_str = f"<{', '.join(args)}>"
        
        return f"{base_type_name}{type_args_str}{dimensions_str}"
        
    def _get_docstring(self, node):
        if node.documentation: # javalang stores doc comment in 'documentation'
            return node.documentation.strip() # Strip leading/trailing whitespace
        return None
        
    def extract_methods(self, file_path):
        tree, content = self.parse_file(file_path)
        if not tree: return []

        methods = []
        # This will find all MethodDeclarations, typically within classes for Java
        for _, node in tree.filter(javalang.tree.MethodDeclaration):
            start_line = node.position.line if node.position else -1
            if start_line == -1: continue

            end_line = self._find_block_end(start_line, content)
            methods.append({
                'name': node.name,
                'signature': self._get_method_signature(node, file_path), # Pass file_path
                'file_path': file_path,
                'start_line': start_line,
                'end_line': end_line,
                'source_code': '\n'.join(content.splitlines()[start_line-1:end_line]),
                'doc_string': self._get_docstring(node)
            })
        
        # Additionally, extract constructors if this method is meant to get all "callable" top-level entities
        # However, constructors are tied to classes, so extract_classes is the primary source.
        # To avoid duplicates if fl.py combines this with extract_classes, be careful.
        # For now, keeping it focused on MethodDeclaration as per its name.
        return methods

    def get_imports(self, file_path):
        """返回映射: 简名 -> 完整限定名，同时保留通配符前缀。"""
        imports = {}
        self.wildcard_imports = []  # e.g. java.util.*
        try:
            tree = self.get_compilation_unit(file_path)
            for _, node in tree.filter(javalang.tree.Import):
                path = node.path
                if node.wildcard:  # import xxx.*;
                    self.wildcard_imports.append(path[:-1] + ".")
                else:
                    short_name = path.split('.')[-1]
                    imports[short_name] = path
            return imports
        except Exception as e:
            print(f"Error while parsing import statements in file {file_path}: {str(e)}")
            return {}

    def get_global_methods(self, file_path, repo_name):
        tree = self.get_compilation_unit(file_path)
        if not tree: return [] 
        
        # Java does not have global methods in the same way Python or C++ (non-class functions) might.
        # All significant methods are within classes or interfaces.
        # These are already extracted by `extract_classes` along with their class context.
        # Returning an empty list here to avoid duplicate processing if `fl.py` (or other callers)
        # try to combine results from `get_global_methods` and methods extracted from `extract_classes`.
        # This also helps in reducing the number of items processed to the actual distinct entities.
        return []

    def get_global_variables(self, file_path, repo_name):
        tree = self.get_compilation_unit(file_path)
        if not tree: return []

        # Similarly to global methods, "global" variables in Java are typically static fields of classes.
        # These are best extracted as part of the class structure by `extract_classes` if needed.
        # Returning an empty list to prevent potential duplicates or misinterpretation of "global".
        return []

    def analyze_method_calls_in_method(self, local_method_info, all_methods, kg, imports, repo_name):
        file_path = local_method_info.get('file_path')
        method_name_to_find = local_method_info.get('name')
        method_start_line = local_method_info.get('start_line')

        if not file_path or not method_name_to_find:
            print("Analyze method calls: Missing file_path or method_name in local_method_info")
            return

        compilation_unit = self.get_compilation_unit(file_path)
        if not compilation_unit:
            print(f"Analyze method calls: Could not get CompilationUnit for {file_path}")
            return
        
        target_method_node = None
        # Find the specific method node in the AST
        # This requires iterating through classes and their methods
        try:
            for path, node in compilation_unit:
                if isinstance(node, (javalang.tree.ClassDeclaration, javalang.tree.InterfaceDeclaration)):
                    # Check regular methods
                    if hasattr(node, 'methods'):
                        for decl_node in node.methods: # node.methods should contain MethodDeclaration
                            if isinstance(decl_node, javalang.tree.MethodDeclaration): 
                                node_start_line = decl_node.position.line if decl_node.position else -1
                                if decl_node.name == method_name_to_find and \
                                   (method_start_line is None or node_start_line == method_start_line):
                                    target_method_node = decl_node
                                    break # Found method, break from inner loop
                    if target_method_node: 
                        break # Found method, break from outer loop

                    # Check constructors (name will be class name)
                    # Iterate through the raw body of the class for constructors
                    if hasattr(node, 'body') and node.body: # Ensure body exists
                        for decl_node in node.body: 
                            if isinstance(decl_node, javalang.tree.ConstructorDeclaration):
                                node_start_line = decl_node.position.line if decl_node.position else -1
                                # Constructor's name in javalang is the class's name.
                                # method_name_to_find from local_method_info should also be class name for constructors.
                                if decl_node.name == method_name_to_find and \
                                   (method_start_line is None or node_start_line == method_start_line):
                                    target_method_node = decl_node
                                    break # Found constructor, break from inner loop
                    if target_method_node: 
                        break # Found constructor, break from outer loop
                
                elif isinstance(node, javalang.tree.EnumDeclaration):
                    if hasattr(node, 'body') and node.body and hasattr(node.body, 'declarations') and node.body.declarations:
                        for body_decl in node.body.declarations:
                            if isinstance(body_decl, javalang.tree.MethodDeclaration):
                                node_start_line = body_decl.position.line if body_decl.position else -1
                                if body_decl.name == method_name_to_find and \
                                   (method_start_line is None or node_start_line == method_start_line):
                                    target_method_node = body_decl
                                    break # Found method in enum, break from inner loop
                            elif isinstance(body_decl, javalang.tree.ConstructorDeclaration):
                                node_start_line = body_decl.position.line if body_decl.position else -1
                                # Enum constructor name is also the enum's name
                                if body_decl.name == method_name_to_find and \
                                   (method_start_line is None or node_start_line == method_start_line):
                                    target_method_node = body_decl
                                    break # Found constructor in enum, break from inner loop
                    if target_method_node: 
                        break # Found in enum, break from outer loop
        except Exception as e:
            print(f"Error while searching for method node: {e}")
            return

        if not target_method_node:
            print(f"Analyze method calls: Could not find MethodOrConstructorDeclaration for '{method_name_to_find}' in {file_path} at line {method_start_line}")
            return

        # Now, traverse the target_method_node for MethodInvocation and ClassCreator nodes
        try:
            for _, invoked_node in target_method_node:
                callee_name_str = None
                is_constructor_call = False

                if isinstance(invoked_node, javalang.tree.MethodInvocation):
                    # Example: qualifier.member() or member() or package.Class.member()
                    # invoked_node.member is the method name string
                    # invoked_node.qualifier can be an identifier, a FQN string, or None
                    method_name_called = invoked_node.member
                    qualifier = invoked_node.qualifier

                    # Ensure method_name_called is a string
                    if not isinstance(method_name_called, str):
                        continue

                    if qualifier: # something.method()
                        # Ensure qualifier is a string before concatenation
                        if not isinstance(qualifier, str):
                            qualifier_str = str(qualifier) 
                        else:
                            qualifier_str = qualifier
                        callee_name_str = f"{qualifier_str}.{method_name_called}"
                    else: # method() - called on current class instance or a static import
                        callee_name_str = method_name_called 

                elif isinstance(invoked_node, javalang.tree.ClassCreator):
                    # Example: new MyClass() or new com.example.MyClass()
                    class_type_node = invoked_node.type
                    if not class_type_node or not hasattr(class_type_node, 'name') or not isinstance(class_type_node.name, str):
                        continue
                    
                    class_name_called = class_type_node.name
                    
                    if hasattr(class_type_node, 'sub_type') and class_type_node.sub_type:
                        if not isinstance(class_type_node.sub_type, str):
                            continue
                        class_name_called = f"{class_name_called}.{class_type_node.sub_type}"
                    
                    callee_name_str = class_name_called 
                    is_constructor_call = True

                if callee_name_str:
                    # TODO: Resolve callee_name_str against imports and current file context to get FQN
                    # This is the hard part: mapping a potentially simple name to its FQN.
                    # For now, we search all_methods using the potentially partial callee_name_str.
                    # A more robust solution would try to build the FQN based on imports and current package/class.

                    for m_info in all_methods: # all_methods is a list of dicts from KG
                        # Direct match or if callee_name_str is FQN and matches
                        if m_info['name'] == callee_name_str or \
                           (is_constructor_call and m_info['name'] == callee_name_str and m_info.get('is_constructor')) or \
                           m_info['name'].endswith('.' + callee_name_str): # Heuristic for simple name match to FQN
                            
                            kg.link_method_calls(
                                caller_method_name=local_method_info['name'],
                                caller_method_signature=local_method_info.get('signature', local_method_info['name']),
                                callee_method_name=m_info['name'],
                                callee_method_signature=m_info.get('signature', m_info['name'])
                            )
                            # Found a match, ideally break if we are sure, but multiple overloads might exist.
                            # For simplicity, link first match. More advanced would check signature.
                            break 
        except Exception as e:
            print(f"Error while analyzing method calls in {file_path}: {e}")

    def analyze_snippet_for_references(self, code_snippet_string: str) -> list[tuple[str, str]]:
        references = []
        if not code_snippet_string.strip():
            return references

        try:
            tokens = list(javalang.tokenizer.tokenize(code_snippet_string))
        except javalang.parser.JavaSyntaxError as e:
            print(f"Java snippet syntax error, falling back to regex for references: {e}")
            return self._analyze_snippet_with_regex(code_snippet_string)

        i = 0
        while i < len(tokens):
            token = tokens[i]

            if token.value == 'import':
                path_parts = []
                is_static = False
                is_wildcard = False
                j = i + 1
                if j < len(tokens) and tokens[j].value == 'static':
                    is_static = True
                    j += 1
                
                while j < len(tokens) and tokens[j].__class__ in (javalang.tokenizer.Identifier, javalang.tokenizer.Separator) and tokens[j].value != ';':
                    if tokens[j].value == '.':
                        pass
                    elif tokens[j].value == '*':
                        is_wildcard = True
                    else:
                        path_parts.append(tokens[j].value)
                    j += 1
                
                if path_parts:
                    full_import_path = ".".join(path_parts)
                    if is_static and is_wildcard:
                        ref_type = 'import_static_package'
                    elif is_static:
                        ref_type = 'import_static_member'
                    elif is_wildcard:
                        ref_type = 'import_package'
                    else:
                        ref_type = 'import_class'
                    references.append((ref_type, full_import_path))
                i = j 
                continue

            is_new_invocation = False
            if token.value == 'new' and i + 1 < len(tokens):
                is_new_invocation = True
                i += 1 
                token = tokens[i]

            if isinstance(token, javalang.tokenizer.Identifier):
                fqn_parts = [token.value]
                j = i + 1
                while j + 1 < len(tokens) and tokens[j].value == '.' and isinstance(tokens[j+1], javalang.tokenizer.Identifier):
                    fqn_parts.append(tokens[j+1].value)
                    j += 2
                
                current_fqn = ".".join(fqn_parts)

                if len(fqn_parts) > 1: 
                    ref_type_detail = 'constructor_call_fqn' if is_new_invocation else 'class_or_package_reference_fqn'
                    references.append((ref_type_detail, current_fqn))
                elif is_new_invocation: 
                    references.append(('constructor_call_simple', current_fqn))

                if j < len(tokens) and tokens[j].value == '.':
                    if j + 1 < len(tokens) and isinstance(tokens[j+1], javalang.tokenizer.Identifier):
                        method_name = tokens[j+1].value
                        if j + 2 < len(tokens) and tokens[j+2].value == '(':
                            references.append(('method_call', f"{current_fqn}.{method_name}"))
                            i = j + 2 
                            continue
                i = j 
                continue
            
            i += 1
        
        ordered_unique_references = []
        seen = set()
        for ref_type, ref_val in references:
            if (ref_type, ref_val) not in seen:
                ordered_unique_references.append((ref_type, ref_val))
                seen.add((ref_type, ref_val))
        
        return ordered_unique_references

    def _analyze_snippet_with_regex(self, code_snippet_string: str) -> list[tuple[str, str]]:
        references = []
        import_pattern = re.compile(r'import\s+(static\s+)?([\w\.]+)(\.\*)?;')
        for match in import_pattern.finditer(code_snippet_string):
            is_static = bool(match.group(1))
            path = match.group(2)
            is_wildcard = bool(match.group(3))
            
            if is_static and is_wildcard:
                ref_type = 'import_static_package'
            elif is_static:
                ref_type = 'import_static_member'
            elif is_wildcard:
                ref_type = 'import_package'
            else:
                ref_type = 'import_class'
            references.append((ref_type, path))

        fqn_pattern = re.compile(r'(?:new\s+)?([a-zA-Z_]\w*(?:\.[\w\L]*)+)(?:\s*\(|\s*\.)')
        for match in fqn_pattern.finditer(code_snippet_string):
            full_name = match.group(1)
            is_constructor = match.group(0).startswith('new')
            is_already_imported_subsegment = False
            for ref_type_seen, ref_val_seen in references:
                if ref_type_seen.startswith('import') and full_name in ref_val_seen:
                    is_already_imported_subsegment = True
                    break
            if not is_already_imported_subsegment:
                ref_type = 'constructor_call_fqn' if is_constructor else 'class_or_package_reference_fqn'
                references.append((ref_type, full_name))
        
        call_pattern = re.compile(r'([A-Za-z_]\w*)\.([a-zA-Z_]\w+)\s*\(')
        for match in call_pattern.finditer(code_snippet_string):
            references.append(('method_call', f"{match.group(1)}.{match.group(2)}"))
        
        simple_constructor_pattern = re.compile(r'new\s+([A-Z][A-Za-z_0-9]*)\s*\(')
        for match in simple_constructor_pattern.finditer(code_snippet_string):
            if not any(ref_val == match.group(1) and ref_type == 'constructor_call_fqn' for ref_type, ref_val in references):
                 references.append(('constructor_call_simple', match.group(1)))

        ordered_unique_references = []
        seen = set()
        for ref_type, ref_val in references:
            if (ref_type, ref_val) not in seen:
                ordered_unique_references.append((ref_type, ref_val))
                seen.add((ref_type, ref_val))
        return ordered_unique_references

class VerilogParser(BaseParser):
    REGION_END = {
        'module': 'endmodule',
        'interface': 'endinterface',
        'package': 'endpackage',
        'program': 'endprogram',
    }

    KEYWORDS = {
        'module', 'endmodule', 'interface', 'endinterface', 'package', 'endpackage',
        'program', 'endprogram', 'input', 'output', 'inout', 'wire', 'reg', 'logic',
        'assign', 'always', 'always_ff', 'always_comb', 'always_latch', 'initial',
        'begin', 'end', 'if', 'else', 'case', 'endcase', 'for', 'while', 'generate',
        'endgenerate', 'function', 'endfunction', 'task', 'endtask', 'parameter',
        'localparam', 'typedef', 'struct', 'enum', 'import', 'automatic',
    }

    def __init__(self):
        super().__init__('verilog')
        self._verilog_ast_extractor = None
        if VerilogAstExtractor is not None:
            try:
                self._verilog_ast_extractor = VerilogAstExtractor()
            except VerilogAstUnavailable as exc:
                print(f"Verilog AST parser unavailable; using regex fallback: {exc}")
            except Exception as exc:
                print(f"Verilog AST parser initialization failed; using regex fallback: {exc}")

    def _mark_parse_metadata(self, value, parse_source='regex', parse_confidence=0.55):
        if isinstance(value, list):
            return [
                self._mark_parse_metadata(item, parse_source, parse_confidence)
                for item in value
            ]
        if not isinstance(value, dict):
            return value
        value.setdefault('parse_source', parse_source)
        value.setdefault('parse_confidence', parse_confidence)
        for key in ('methods', 'rtl_entities', 'entities'):
            if isinstance(value.get(key), list):
                value[key] = self._mark_parse_metadata(value[key], parse_source, parse_confidence)
        for key in ('method_signal_edges', 'signal_edges', 'instance_edges'):
            if isinstance(value.get(key), list):
                value[key] = self._mark_parse_metadata(value[key], parse_source, 0.5)
        return value

    def _clean_path(self, file_path: str) -> str:
        path = os.path.normpath(file_path).replace('\\', '/')
        if os.path.isabs(path):
            try:
                path = os.path.relpath(path, os.getcwd()).replace('\\', '/')
            except ValueError:
                pass
        if path.startswith('workdirs/'):
            parts = path.split('/')
            if len(parts) > 4 and parts[2] == 'repos':
                return '/'.join(parts[4:])
        for marker in ('playground', 'verilog_repair_cases'):
            prefix = marker + '/'
            if path.startswith(prefix):
                parts = path.split('/')
                if len(parts) > 2:
                    return '/'.join(parts[2:])
            marker_idx = path.find('/' + prefix)
            if marker_idx >= 0:
                parts = path[marker_idx + 1:].split('/')
                if len(parts) > 2:
                    return '/'.join(parts[2:])
        return path

    def _read_file(self, file_path):
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()

    def _strip_comments_keep_lines(self, content: str) -> str:
        result = []
        i = 0
        in_block = False
        while i < len(content):
            ch = content[i]
            nxt = content[i:i + 2]
            if in_block:
                if nxt == '*/':
                    in_block = False
                    result.extend('  ')
                    i += 2
                else:
                    result.append('\n' if ch == '\n' else ' ')
                    i += 1
            elif nxt == '/*':
                in_block = True
                result.extend('  ')
                i += 2
            elif nxt == '//':
                while i < len(content) and content[i] != '\n':
                    result.append(' ')
                    i += 1
            else:
                result.append(ch)
                i += 1
        return ''.join(result)

    def parse_file(self, file_path):
        content = self._read_file(file_path)
        return self._strip_comments_keep_lines(content), content

    def get_compilation_unit(self, file_path: str):
        if file_path not in self._file_ast_cache:
            self._file_ast_cache[file_path] = self.parse_file(file_path)
        return self._file_ast_cache[file_path]

    def _find_regions(self, file_path):
        cleaned, content = self.parse_file(file_path)
        clean_file_path = self._clean_path(file_path)
        lines = cleaned.splitlines()
        raw_lines = content.splitlines()
        regions = []
        start_re = re.compile(r'^\s*(module|interface|package|program)\s+([A-Za-z_][\w$]*)\b')
        idx = 0
        while idx < len(lines):
            match = start_re.search(lines[idx])
            if not match:
                idx += 1
                continue
            kind, name = match.group(1), match.group(2)
            end_kw = self.REGION_END[kind]
            end_idx = idx
            for scan_idx in range(idx + 1, len(lines)):
                if re.search(rf'\b{end_kw}\b', lines[scan_idx]):
                    end_idx = scan_idx
                    break
            source = '\n'.join(raw_lines[idx:end_idx + 1])
            regions.append({
                'kind': kind,
                'name': name,
                'file_path': clean_file_path,
                'start_line': idx + 1,
                'end_line': end_idx + 1,
                'source_code': source,
                'doc_string': self._leading_comment(raw_lines, idx),
                'cleaned_source': '\n'.join(lines[idx:end_idx + 1]),
            })
            idx = end_idx + 1
        return regions

    def _leading_comment(self, lines, start_idx):
        comments = []
        idx = start_idx - 1
        while idx >= 0:
            stripped = lines[idx].strip()
            if stripped.startswith('//'):
                comments.insert(0, stripped[2:].strip())
                idx -= 1
                continue
            if stripped == '':
                idx -= 1
                continue
            break
        return ' '.join(comments)

    def _find_statement_end(self, lines, start_idx):
        balance = 0
        saw_begin = False
        for idx in range(start_idx, len(lines)):
            line = lines[idx]
            begin_count = len(re.findall(r'\bbegin\b', line))
            end_count = len(re.findall(r'\bend\b', line)) - len(re.findall(r'\b(?:endmodule|endcase|endgenerate|endfunction|endtask|endinterface|endpackage|endprogram)\b', line))
            if begin_count:
                saw_begin = True
            balance += begin_count
            balance -= max(end_count, 0)
            if saw_begin and balance <= 0 and idx > start_idx:
                return idx
            if not saw_begin and ';' in line:
                return idx
        return start_idx

    def _find_end_keyword(self, lines, start_idx, end_keyword):
        for idx in range(start_idx + 1, len(lines)):
            if re.search(rf'\b{end_keyword}\b', lines[idx]):
                return idx
        return start_idx

    def _identifier_from_declaration(self, line):
        before_semicolon = line.split(';', 1)[0]
        identifiers = re.findall(r'\b[A-Za-z_][\w$]*\b', before_semicolon)
        identifiers = [item for item in identifiers if item not in self.KEYWORDS]
        return identifiers[-1] if identifiers else None

    def _base_identifier(self, value):
        match = re.search(r'\b[A-Za-z_][\w$]*\b', value or '')
        return match.group(0) if match else ''

    def _extract_identifiers(self, text):
        identifiers = set(re.findall(r'\b[A-Za-z_][\w$]*\b', text or ''))
        return {
            item for item in identifiers
            if item not in self.KEYWORDS
            and item not in {'posedge', 'negedge', 'signed', 'unsigned'}
            and not item[0].isdigit()
        }

    def _extract_macro_references(self, text):
        macro_keywords = {'include', 'define', 'ifdef', 'ifndef', 'elsif', 'else', 'endif', 'undef'}
        return {
            item for item in re.findall(r'`([A-Za-z_][\w$]*)', text or '')
            if item not in macro_keywords
        }

    def _strip_decl_modifiers(self, text):
        text = re.sub(r'\b(?:wire|reg|logic|signed|unsigned|integer|bit)\b', ' ', text)
        text = re.sub(r'\[[^\]]+\]', ' ', text)
        return text

    def _split_decl_names(self, text):
        names = []
        for part in text.split(','):
            part = part.strip().rstrip(');')
            if not part:
                continue
            part = part.split('=', 1)[0].strip()
            part = re.sub(r'\[[^\]]+\]', ' ', part)
            identifiers = [
                item for item in re.findall(r'\b[A-Za-z_][\w$]*\b', part)
                if item not in self.KEYWORDS
                and item not in {'wire', 'reg', 'logic', 'signed', 'unsigned', 'integer', 'bit'}
            ]
            if identifiers:
                names.append(identifiers[-1])
        return names

    def _parse_declaration_line(self, line):
        stripped = line.strip()
        if not stripped:
            return []

        stripped = stripped.rstrip(',;')
        first = stripped.split()[0] if stripped.split() else ''
        if first in {'input', 'output', 'inout'}:
            label = 'Port'
            rtl_kind = 'port'
            direction = first
            rest = stripped[len(first):].strip()
        elif first in {'wire', 'reg', 'logic'}:
            label = 'Signal'
            rtl_kind = first
            direction = ''
            rest = stripped[len(first):].strip()
        elif first in {'parameter', 'localparam'}:
            label = 'Parameter'
            rtl_kind = first
            direction = ''
            rest = stripped[len(first):].strip()
        else:
            return []

        width_match = re.search(r'\[[^\]]+\]', rest)
        width = width_match.group(0) if width_match else ''
        rest = self._strip_decl_modifiers(rest)
        names = self._split_decl_names(rest)
        return [
            {
                'label': label,
                'rtl_kind': rtl_kind,
                'direction': direction,
                'width': width,
                'signal_name': name,
                'declaration': line.strip(),
            }
            for name in names
        ]

    def _extract_region_entities(self, region):
        source_lines = region['source_code'].splitlines()
        cleaned_lines = region['cleaned_source'].splitlines()
        module_name = region['name']
        file_path = region['file_path']
        start_offset = region['start_line'] - 1
        entities = []
        signal_index = {}
        seen = set()

        for idx, line in enumerate(cleaned_lines):
            for decl in self._parse_declaration_line(line):
                signal_name = decl['signal_name']
                entity_name = f"{module_name}.{signal_name}"
                key = (decl['label'], entity_name)
                if key in seen:
                    continue
                seen.add(key)
                entity = {
                    'label': decl['label'],
                    'name': entity_name,
                    'file_path': file_path,
                    'module_name': module_name,
                    'rtl_kind': decl['rtl_kind'],
                    'verilog_kind': decl['rtl_kind'],
                    'signal_name': signal_name,
                    'direction': decl['direction'],
                    'width': decl['width'],
                    'declaration': decl['declaration'],
                    'start_line': start_offset + idx + 1,
                    'end_line': start_offset + idx + 1,
                    'source_code': source_lines[idx] if idx < len(source_lines) else decl['declaration'],
                }
                entity['semantic_summary'] = (
                    f"{decl['label']} {entity_name}; module={module_name}; "
                    f"kind={decl['rtl_kind']}; direction={decl['direction'] or 'internal'}; "
                    f"width={decl['width'] or 'scalar'}; declaration={decl['declaration']}"
                )
                entities.append(entity)
                signal_index[signal_name] = entity

        return entities, signal_index

    def _extract_macros(self, file_path):
        content = self._read_file(file_path)
        clean_file_path = self._clean_path(file_path)
        entities = []
        for idx, line in enumerate(content.splitlines(), 1):
            define_match = re.match(r'\s*`define\s+([A-Za-z_][\w$]*)\b(.*)', line)
            if not define_match:
                continue
            macro = define_match.group(1)
            value = define_match.group(2).strip()
            name = f"`{macro}"
            entities.append({
                'label': 'Macro',
                'name': name,
                'file_path': clean_file_path,
                'module_name': '',
                'rtl_kind': 'macro',
                'verilog_kind': 'macro',
                'signal_name': macro,
                'direction': '',
                'width': '',
                'declaration': line.strip(),
                'start_line': idx,
                'end_line': idx,
                'source_code': line,
                'semantic_summary': f"Macro {name}`; value={value}; declaration={line.strip()}",
            })
        return entities

    def _analyze_signal_usage(self, source, signal_index, verilog_kind, assign_target=None):
        signal_names = set(signal_index.keys())
        if verilog_kind == 'module_body':
            return {'reads': set(), 'writes': set(), 'drives': set(), 'connects': set(), 'feeds': set()}
        if not signal_names:
            return {'reads': set(), 'writes': set(), 'drives': set(), 'connects': set(), 'feeds': set()}

        cleaned = self._strip_comments_keep_lines(source)
        identifiers = self._extract_identifiers(cleaned)
        writes = set()
        drives = set()
        connects = set()

        if verilog_kind == 'assign':
            target_name = self._base_identifier(assign_target or cleaned.split('=', 1)[0])
            if target_name in signal_names:
                drives.add(target_name)
                writes.add(target_name)
        elif verilog_kind == 'instance':
            for match in re.finditer(r'\.[A-Za-z_][\w$]*\s*\(([^)]*)\)', cleaned):
                connects.update(self._extract_identifiers(match.group(1)) & signal_names)
        elif verilog_kind not in {'module_body'}:
            for match in re.finditer(r'\b([A-Za-z_][\w$]*)\b\s*(?:\[[^\]]+\])?\s*(?:<=|=)', cleaned):
                candidate = match.group(1)
                if candidate in signal_names:
                    writes.add(candidate)

        read_candidates = (identifiers & signal_names) - writes - drives
        if verilog_kind == 'instance':
            read_candidates -= connects

        feeds = set()
        for source_signal in read_candidates | connects:
            for target_signal in writes | drives:
                if source_signal != target_signal:
                    feeds.add((source_signal, target_signal))

        return {
            'reads': read_candidates,
            'writes': writes,
            'drives': drives,
            'connects': connects,
            'feeds': feeds,
        }

    def _method_semantic_summary(self, module_name, verilog_kind, signature, source, signal_index, assign_target=None, module_type=None, instance_name=None):
        usage = self._analyze_signal_usage(source, signal_index, verilog_kind, assign_target)
        pieces = [
            f"module={module_name}",
            f"kind={verilog_kind}",
            f"signature={signature}",
        ]
        if assign_target:
            pieces.append(f"drives={self._base_identifier(assign_target)}")
        if module_type:
            pieces.append(f"instantiates={module_type}")
        if instance_name:
            pieces.append(f"instance={instance_name}")
        for key in ('reads', 'writes', 'drives', 'connects'):
            values = sorted(usage[key])
            if values:
                pieces.append(f"{key}={', '.join(values)}")
        macro_refs = sorted(self._extract_macro_references(source))
        if macro_refs:
            pieces.append(f"macros={', '.join('`' + item for item in macro_refs)}")
        compact_source = ' '.join(line.strip() for line in source.splitlines()[:6] if line.strip())
        if compact_source:
            pieces.append(f"source_excerpt={compact_source[:500]}")
        return '; '.join(pieces)

    def _class_semantic_summary(self, region, entities):
        ports = [e for e in entities if e['label'] == 'Port']
        signals = [e for e in entities if e['label'] == 'Signal']
        params = [e for e in entities if e['label'] == 'Parameter']
        header = ' '.join(line.strip() for line in region['source_code'].splitlines()[:12] if line.strip())
        return (
            f"{region['kind']} {region['name']}; "
            f"ports={', '.join(e['signal_name'] for e in ports[:20])}; "
            f"signals={', '.join(e['signal_name'] for e in signals[:20])}; "
            f"parameters={', '.join(e['signal_name'] for e in params[:20])}; "
            f"header={header[:700]}"
        )

    def _extract_module_members(self, region, signal_index=None):
        signal_index = signal_index or {}
        source_lines = region['source_code'].splitlines()
        cleaned_lines = region['cleaned_source'].splitlines()
        module_name = region['name']
        file_path = region['file_path']
        start_offset = region['start_line'] - 1
        members = [{
            'name': f"{module_name}.module",
            'signature': f"{region['kind']} {module_name}",
            'file_path': file_path,
            'start_line': region['start_line'],
            'end_line': region['end_line'],
            'source_code': region['source_code'],
            'doc_string': region.get('doc_string', ''),
            'verilog_kind': 'module_body',
            'semantic_summary': self._method_semantic_summary(
                module_name,
                'module_body',
                f"{region['kind']} {module_name}",
                region['source_code'],
                signal_index,
            ),
        }]

        for idx, line in enumerate(cleaned_lines):
            stripped = line.strip()
            if not stripped:
                continue

            if re.match(r'^(function|task)\b', stripped):
                kind = stripped.split()[0]
                name = self._identifier_from_declaration(stripped)
                if not name:
                    continue
                end_idx = self._find_end_keyword(cleaned_lines, idx, f"end{kind}")
                members.append({
                    'name': f"{module_name}.{name}",
                    'signature': f"{kind} {module_name}.{name}",
                    'file_path': file_path,
                    'start_line': start_offset + idx + 1,
                    'end_line': start_offset + end_idx + 1,
                    'source_code': '\n'.join(source_lines[idx:end_idx + 1]),
                    'doc_string': self._leading_comment(source_lines, idx),
                    'verilog_kind': kind,
                    'semantic_summary': self._method_semantic_summary(
                        module_name,
                        kind,
                        f"{kind} {module_name}.{name}",
                        '\n'.join(source_lines[idx:end_idx + 1]),
                        signal_index,
                    ),
                })
                continue

            if re.match(r'^(always|always_ff|always_comb|always_latch|initial)\b', stripped):
                kind = stripped.split()[0]
                end_idx = self._find_statement_end(cleaned_lines, idx)
                members.append({
                    'name': f"{module_name}.{kind}@{start_offset + idx + 1}",
                    'signature': f"{module_name}.{kind}@{start_offset + idx + 1}",
                    'file_path': file_path,
                    'start_line': start_offset + idx + 1,
                    'end_line': start_offset + end_idx + 1,
                    'source_code': '\n'.join(source_lines[idx:end_idx + 1]),
                    'doc_string': self._leading_comment(source_lines, idx),
                    'verilog_kind': kind,
                    'semantic_summary': self._method_semantic_summary(
                        module_name,
                        kind,
                        f"{module_name}.{kind}@{start_offset + idx + 1}",
                        '\n'.join(source_lines[idx:end_idx + 1]),
                        signal_index,
                    ),
                })
                continue

            assign_match = re.match(r'^assign\s+([A-Za-z_][\w$]*(?:\[[^\]]+\])?)\s*=', stripped)
            if assign_match:
                target = assign_match.group(1)
                target_name = self._base_identifier(target) or target
                end_idx = self._find_statement_end(cleaned_lines, idx)
                members.append({
                    'name': f"{module_name}.assign.{target_name}@{start_offset + idx + 1}",
                    'signature': f"assign {target}",
                    'file_path': file_path,
                    'start_line': start_offset + idx + 1,
                    'end_line': start_offset + end_idx + 1,
                    'source_code': '\n'.join(source_lines[idx:end_idx + 1]),
                    'doc_string': self._leading_comment(source_lines, idx),
                    'verilog_kind': 'assign',
                    'target_signal': target_name,
                    'semantic_summary': self._method_semantic_summary(
                        module_name,
                        'assign',
                        f"assign {target}",
                        '\n'.join(source_lines[idx:end_idx + 1]),
                        signal_index,
                        assign_target=target,
                    ),
                })
                continue

            inst_match = re.match(r'^([A-Za-z_][\w$]*)\s+(?:#\s*\([^;]*?\)\s*)?([A-Za-z_][\w$]*)\s*\(', stripped)
            if inst_match:
                module_type = inst_match.group(1)
                if module_type in self.KEYWORDS:
                    continue
                instance_name = self._extract_instance_name(stripped, module_type)
                if not instance_name:
                    continue
                end_idx = self._find_statement_end(cleaned_lines, idx)
                members.append({
                    'name': f"{module_name}.inst.{module_type}.{instance_name}",
                    'signature': f"{module_name} instantiates {module_type} as {instance_name}",
                    'file_path': file_path,
                    'start_line': start_offset + idx + 1,
                    'end_line': start_offset + end_idx + 1,
                    'source_code': '\n'.join(source_lines[idx:end_idx + 1]),
                    'doc_string': '',
                    'verilog_kind': 'instance',
                    'instance_module': module_type,
                    'instance_name': instance_name,
                    'semantic_summary': self._method_semantic_summary(
                        module_name,
                        'instance',
                        f"{module_name} instantiates {module_type} as {instance_name}",
                        '\n'.join(source_lines[idx:end_idx + 1]),
                        signal_index,
                        module_type=module_type,
                        instance_name=instance_name,
                    ),
                })

        return members

    def _extract_instance_name(self, line, module_type):
        without_type = line[len(module_type):].strip()
        if without_type.startswith('#'):
            paren = 0
            for idx, ch in enumerate(without_type):
                if ch == '(':
                    paren += 1
                elif ch == ')':
                    paren -= 1
                    if paren == 0:
                        without_type = without_type[idx + 1:].strip()
                        break
        match = re.match(r'([A-Za-z_][\w$]*)\s*\(', without_type)
        return match.group(1) if match else None

    def _extract_classes_regex(self, file_path):
        classes = []
        for region in self._find_regions(file_path):
            entities, signal_index = self._extract_region_entities(region)
            classes.append({
                'name': region['name'],
                'file_path': region['file_path'],
                'start_line': region['start_line'],
                'end_line': region['end_line'],
                'source_code': region['source_code'],
                'doc_string': region.get('doc_string', ''),
                'verilog_kind': region['kind'],
                'semantic_summary': self._class_semantic_summary(region, entities),
                'rtl_entities': entities,
                'methods': self._extract_module_members(region, signal_index),
            })
        return classes

    def extract_classes(self, file_path):
        if self._verilog_ast_extractor is not None:
            try:
                return self._verilog_ast_extractor.extract_classes(file_path)
            except Exception as exc:
                print(f"Verilog AST extraction failed for {file_path}; using regex fallback: {exc}")
        return self._mark_parse_metadata(self._extract_classes_regex(file_path), 'regex', 0.55)

    def extract_methods(self, file_path):
        methods = []
        for class_info in self.extract_classes(file_path):
            methods.extend(class_info.get('methods', []))
        return methods

    def get_imports(self, file_path):
        content = self._read_file(file_path)
        imports = {}
        for match in re.finditer(r'`include\s+"([^"]+)"', content):
            include_path = match.group(1)
            imports[os.path.basename(include_path)] = include_path
        for match in re.finditer(r'\bimport\s+([A-Za-z_][\w$]*(?:::[A-Za-z_*][\w$*]*)?)\s*;', content):
            imported = match.group(1)
            imports[imported.split('::')[0]] = imported
        return imports

    def get_global_methods(self, file_path, repo_name):
        return []

    def get_global_variables(self, file_path, repo_name):
        content = self._read_file(file_path)
        clean_file_path = self._clean_path(file_path)
        variables = []
        for idx, line in enumerate(content.splitlines(), 1):
            define_match = re.match(r'\s*`define\s+([A-Za-z_][\w$]*)\b(.*)', line)
            if define_match:
                macro = define_match.group(1)
                variables.append({
                    'name': f"`{macro}",
                    'signature': line.strip(),
                    'file_path': clean_file_path,
                    'start_line': idx,
                    'end_line': idx,
                    'source_code': line,
                    'doc_string': '',
                    'verilog_kind': 'macro',
                    'parse_source': 'regex',
                    'parse_confidence': 0.55,
                    'semantic_summary': f"Macro `{macro}`; declaration={line.strip()}",
                })
        return variables

    def _extract_rtl_graph_regex(self, file_path):
        entities = []
        method_signal_edges = []
        signal_edges = []
        instance_edges = []
        entity_keys = set()

        for macro in self._extract_macros(file_path):
            key = (macro['label'], macro['name'], macro['file_path'], macro.get('module_name', ''))
            if key not in entity_keys:
                entity_keys.add(key)
                entities.append(macro)

        for region in self._find_regions(file_path):
            region_entities, signal_index = self._extract_region_entities(region)
            for entity in region_entities:
                key = (entity['label'], entity['name'], entity['file_path'], entity.get('module_name', ''))
                if key not in entity_keys:
                    entity_keys.add(key)
                    entities.append(entity)
            methods = self._extract_module_members(region, signal_index)

            for method in methods:
                verilog_kind = method.get('verilog_kind', '')
                if verilog_kind == 'module_body':
                    continue
                usage = self._analyze_signal_usage(
                    method.get('source_code', ''),
                    signal_index,
                    verilog_kind,
                    assign_target=method.get('target_signal'),
                )

                for macro_name in sorted(self._extract_macro_references(method.get('source_code', ''))):
                    macro_entity = {
                        'label': 'Macro',
                        'name': f"`{macro_name}",
                        'file_path': region['file_path'],
                        'module_name': region['name'],
                        'rtl_kind': 'macro_ref',
                        'verilog_kind': 'macro_ref',
                        'signal_name': macro_name,
                        'direction': '',
                        'width': '',
                        'declaration': f"`{macro_name}",
                        'start_line': method.get('start_line'),
                        'end_line': method.get('start_line'),
                        'source_code': f"`{macro_name}",
                        'semantic_summary': f"Macro reference `{macro_name}`; module={region['name']}; used_by={method['name']}",
                    }
                    key = (
                        macro_entity['label'],
                        macro_entity['name'],
                        macro_entity['file_path'],
                        macro_entity.get('module_name', ''),
                    )
                    if key not in entity_keys:
                        entity_keys.add(key)
                        entities.append(macro_entity)
                    method_signal_edges.append({
                        'method_name': method['name'],
                        'method_signature': method['signature'],
                        'method_file_path': method['file_path'],
                        'target_label': 'Macro',
                        'target_name': macro_entity['name'],
                        'target_file_path': macro_entity['file_path'],
                        'target_module_name': macro_entity['module_name'],
                        'description': 'uses macro',
                        'reverse_description': 'used by method',
                        'relation_kind': 'mentions',
                    })

                for signal_name in sorted(usage['reads']):
                    entity = signal_index[signal_name]
                    method_signal_edges.append({
                        'method_name': method['name'],
                        'method_signature': method['signature'],
                        'method_file_path': method['file_path'],
                        'target_label': entity['label'],
                        'target_name': entity['name'],
                        'target_file_path': entity['file_path'],
                        'target_module_name': entity['module_name'],
                        'description': 'reads signal',
                        'reverse_description': 'read by method',
                        'relation_kind': 'reads',
                    })

                for signal_name in sorted(usage['writes']):
                    entity = signal_index[signal_name]
                    method_signal_edges.append({
                        'method_name': method['name'],
                        'method_signature': method['signature'],
                        'method_file_path': method['file_path'],
                        'target_label': entity['label'],
                        'target_name': entity['name'],
                        'target_file_path': entity['file_path'],
                        'target_module_name': entity['module_name'],
                        'description': 'writes signal',
                        'reverse_description': 'written by method',
                        'relation_kind': 'writes',
                    })

                for signal_name in sorted(usage['drives']):
                    entity = signal_index[signal_name]
                    method_signal_edges.append({
                        'method_name': method['name'],
                        'method_signature': method['signature'],
                        'method_file_path': method['file_path'],
                        'target_label': entity['label'],
                        'target_name': entity['name'],
                        'target_file_path': entity['file_path'],
                        'target_module_name': entity['module_name'],
                        'description': 'drives signal',
                        'reverse_description': 'driven by method',
                        'relation_kind': 'drives',
                    })

                for signal_name in sorted(usage['connects']):
                    entity = signal_index[signal_name]
                    method_signal_edges.append({
                        'method_name': method['name'],
                        'method_signature': method['signature'],
                        'method_file_path': method['file_path'],
                        'target_label': entity['label'],
                        'target_name': entity['name'],
                        'target_file_path': entity['file_path'],
                        'target_module_name': entity['module_name'],
                        'description': 'connects signal',
                        'reverse_description': 'connected by instance',
                        'relation_kind': 'connects',
                    })

                for source_signal, target_signal in sorted(usage['feeds']):
                    source_entity = signal_index[source_signal]
                    target_entity = signal_index[target_signal]
                    signal_edges.append({
                        'source_label': source_entity['label'],
                        'source_name': source_entity['name'],
                        'source_file_path': source_entity['file_path'],
                        'source_module_name': source_entity['module_name'],
                        'target_label': target_entity['label'],
                        'target_name': target_entity['name'],
                        'target_file_path': target_entity['file_path'],
                        'target_module_name': target_entity['module_name'],
                        'description': 'feeds signal',
                        'reverse_description': 'fed by signal',
                        'relation_kind': 'feeds',
                    })

                if verilog_kind == 'instance' and method.get('instance_module'):
                    instance_edges.append({
                        'method_name': method['name'],
                        'method_signature': method['signature'],
                        'method_file_path': method['file_path'],
                        'class_name': method['instance_module'],
                        'description': 'instantiates module',
                        'reverse_description': 'instantiated by method',
                        'relation_kind': 'instantiates',
                    })

        return {
            'entities': entities,
            'method_signal_edges': method_signal_edges,
            'signal_edges': signal_edges,
            'instance_edges': instance_edges,
        }

    def extract_rtl_graph(self, file_path):
        if self._verilog_ast_extractor is not None:
            try:
                return self._verilog_ast_extractor.extract_rtl_graph(file_path)
            except Exception as exc:
                print(f"Verilog AST RTL graph extraction failed for {file_path}; using regex fallback: {exc}")
        return self._mark_parse_metadata(self._extract_rtl_graph_regex(file_path), 'regex', 0.55)

    def _extract_instantiations_from_source(self, source):
        instantiations = []
        for line in self._strip_comments_keep_lines(source).splitlines():
            stripped = line.strip()
            match = re.match(r'^([A-Za-z_][\w$]*)\s+(?:#\s*\([^;]*?\)\s*)?([A-Za-z_][\w$]*)\s*\(', stripped)
            if match and match.group(1) not in self.KEYWORDS:
                instantiations.append((match.group(1), match.group(2)))
        return instantiations

    def analyze_method_calls_in_method(self, local_method_info, all_methods, kg, imports, repo_name):
        source = local_method_info.get('source_code', '')
        if not source:
            return
        caller_name = local_method_info.get('name')
        caller_signature = local_method_info.get('signature', caller_name)
        if not caller_name or not caller_signature:
            return

        linked = set()
        for module_type, _instance_name in self._extract_instantiations_from_source(source):
            target_suffix = f"{module_type}.module"
            for method in all_methods:
                if method['name'] == target_suffix or method['name'].endswith('.' + target_suffix):
                    key = (caller_name, method['name'])
                    if key in linked or caller_name == method['name']:
                        continue
                    linked.add(key)
                    kg.link_method_calls(caller_name, caller_signature, method['name'], method.get('signature', method['name']))
                    break

        for method in all_methods:
            callee_name = method.get('name', '')
            simple_name = callee_name.split('.')[-1].split('@')[0]
            if not simple_name or simple_name in {'module'}:
                continue
            if re.search(rf'\b{re.escape(simple_name)}\s*\(', source) and caller_name != callee_name:
                key = (caller_name, callee_name)
                if key in linked:
                    continue
                linked.add(key)
                kg.link_method_calls(caller_name, caller_signature, callee_name, method.get('signature', callee_name))

    def analyze_snippet_for_references(self, code_snippet_string):
        references = []
        for match in re.finditer(r'([A-Za-z0-9_./\\-]+\.(?:v|sv|vh|svh))\b', code_snippet_string):
            references.append(('file', match.group(1).replace('\\', '/')))
        for match in re.finditer(r'\b(?:module|interface|package|program)\s+([A-Za-z_][\w$]*)\b', code_snippet_string):
            references.append(('module', match.group(1)))
        for module_type, _instance_name in self._extract_instantiations_from_source(code_snippet_string):
            references.append(('module_instantiation', module_type))
        for match in re.finditer(r'\b(?:always_ff|always_comb|always_latch|always|initial|assign|function|task)\b', code_snippet_string):
            references.append(('hdl_construct', match.group(0)))

        ordered = []
        seen = set()
        for ref in references:
            if ref not in seen:
                ordered.append(ref)
                seen.add(ref)
        return ordered

class ParserFactory:
    @staticmethod
    def create_parser(language):
        if language == 'python':
            return PythonParser()
        elif language == 'cpp':
            return CppParser()
        elif language == 'java':
            return JavaParser()
        elif language == 'verilog':
            return VerilogParser()
        else:
            raise ValueError(f"Unsupported language: {language}") 
