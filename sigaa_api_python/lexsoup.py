import re
from selectolax.lexbor import LexborHTMLParser
_RE_TYPE = type(re.compile(''))

class NavigableText(str):

    def __new__(cls, value, parent):
        obj = super().__new__(cls, value)
        obj.parent = parent
        obj.name = None
        return obj

def _attr_value(raw):
    return '' if raw is None else raw

class Tag:
    __slots__ = ('_node',)

    def __init__(self, node):
        self._node = node

    def __eq__(self, other):
        return isinstance(other, Tag) and self._node.mem_id == other._node.mem_id

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self._node.mem_id)

    def __bool__(self):
        return True

    def __repr__(self):
        return f'<lexsoup.Tag {self.name}>'

    def __str__(self):
        return self._node.html or ''

    @property
    def name(self):
        return self._node.tag

    @property
    def parent(self):
        p = self._node.parent
        if p is None or not p.is_element_node:
            return None
        return Tag(p)

    @property
    def attrs(self):
        out = {}
        for k, v in self._node.attributes.items():
            out[k] = _attr_value(v).split() if k == 'class' else _attr_value(v)
        return out

    def get(self, key, default=None):
        attrs = self._node.attributes
        if key not in attrs:
            return default
        value = _attr_value(attrs[key])
        return value.split() if key == 'class' else value

    def __getitem__(self, key):
        attrs = self._node.attributes
        if key not in attrs:
            raise KeyError(key)
        value = _attr_value(attrs[key])
        return value.split() if key == 'class' else value

    def has_attr(self, key):
        return key in self._node.attributes

    def get_text(self, separator='', strip=False):
        pieces = []
        for n in self._iter_text_descendants():
            text = n.text_content or ''
            if strip:
                text = text.strip()
                if not text:
                    continue
            pieces.append(text)
        return separator.join(pieces)

    @property
    def text(self):
        return self.get_text()

    @property
    def string(self):
        children = [c for c in self._node.iter(include_text=True) if not c.is_comment_node]
        if len(children) != 1:
            return None
        only = children[0]
        if only.is_text_node:
            return NavigableText(only.text_content or '', Tag(self._node))
        if only.is_element_node:
            return Tag(only).string
        return None

    def _iter_descendants(self, include_text=False):
        self_id = self._node.mem_id
        for n in self._node.traverse(include_text=include_text):
            if n.mem_id == self_id:
                continue
            yield n

    def _iter_text_descendants(self):
        for n in self._iter_descendants(include_text=True):
            if n.is_text_node:
                yield n

    def find(self, name=None, attrs=None, string=None, **kwargs):
        results = self._find_engine(name, attrs, string, kwargs, first_only=True)
        return results[0] if results else None

    def find_all(self, name=None, attrs=None, string=None, **kwargs):
        return self._find_engine(name, attrs, string, kwargs, first_only=False)

    def _find_engine(self, name, attrs, string, kwargs, first_only):
        filters = dict(attrs) if attrs else {}
        for key, value in kwargs.items():
            filters['class' if key == 'class_' else key] = value
        string_matcher = _make_string_matcher(string) if string is not None else None
        if name is None and (not filters) and (string_matcher is not None):
            results = []
            for n in self._iter_text_descendants():
                text = n.text_content or ''
                if string_matcher(text):
                    parent = n.parent
                    results.append(NavigableText(text, Tag(parent) if parent is not None and parent.is_element_node else None))
                    if first_only:
                        break
            return results
        name_matcher = _make_name_matcher(name)
        attr_matchers = {k: _make_attr_matcher(k, v) for k, v in filters.items()}
        results = []
        for n in self._iter_descendants():
            if not n.is_element_node:
                continue
            tag = Tag(n)
            if not name_matcher(tag):
                continue
            node_attrs = n.attributes
            ok = True
            for key, matcher in attr_matchers.items():
                raw = node_attrs.get(key) if key in node_attrs else None
                present = key in node_attrs
                if not matcher(present, _attr_value(raw) if present else None):
                    ok = False
                    break
            if not ok:
                continue
            if string_matcher is not None:
                tag_string = tag.string
                if tag_string is None or not string_matcher(tag_string):
                    continue
            results.append(tag)
            if first_only:
                break
        return results

    def find_parent(self, name=None):
        p = self.parent
        name_matcher = _make_name_matcher(name)
        while p is not None:
            if name_matcher(p):
                return p
            p = p.parent
        return None

    def find_next(self, name=None):
        parser = self._node.parser
        root = parser.root if parser is not None else None
        if root is None:
            return None
        name_matcher = _make_name_matcher(name)
        self_id = self._node.mem_id
        seen_self = False
        for n in root.traverse(include_text=False):
            if n.mem_id == self_id:
                seen_self = True
                continue
            if not seen_self or not n.is_element_node:
                continue
            tag = Tag(n)
            if name_matcher(tag):
                return tag
        return None

    def select(self, selector):
        return [Tag(n) for n in self._node.css(selector)]

    def select_one(self, selector):
        n = self._node.css_first(selector)
        return Tag(n) if n is not None else None

    def decompose(self):
        self._node.decompose()

def _make_name_matcher(name):
    if name is None:
        return lambda tag: True
    if callable(name) and (not isinstance(name, _RE_TYPE)):
        return lambda tag: bool(name(tag))
    return lambda tag: tag.name == name

def _make_string_matcher(matcher):
    if isinstance(matcher, _RE_TYPE):
        return lambda text: bool(matcher.search(text))
    if callable(matcher):
        return lambda text: bool(matcher(text))
    return lambda text: text == matcher

def _make_attr_matcher(key, want):
    is_class = key == 'class'

    def match(present, value):
        if want is True:
            return present
        if isinstance(want, _RE_TYPE):
            return present and bool(want.search(value))
        if callable(want):
            return bool(want(value if present else None))
        if isinstance(want, (list, tuple, set)):
            if not present:
                return False
            if is_class:
                classes = value.split()
                return any((w in classes for w in want))
            return value in want
        if not present:
            return False
        if is_class:
            return want in value.split() or want == value
        return value == want
    return match

class LexSoup(Tag):
    __slots__ = ('_parser',)

    def __init__(self, html):
        parser = LexborHTMLParser(html if html else '<html></html>')
        if parser.root is None:
            parser = LexborHTMLParser('<html></html>')
        self._parser = parser
        super().__init__(parser.root)

    def __repr__(self):
        return '<LexSoup document>'