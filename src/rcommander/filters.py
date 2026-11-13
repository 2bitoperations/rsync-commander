"""rsync-style include/exclude rules.

Rules are evaluated in order and the first match wins; a path no rule matches is
included. Each rule is "+ PATTERN" (include) or "- PATTERN" (exclude).

Patterns match the path relative to the source root, '/'-separated:
  *       any run of characters except '/'
  **      anything, including '/'  ('**/' also matches zero directories)
  ?       one character except '/'
  [abc]   character class
  A pattern without a '/' matches the last path component at any depth
  (rsync semantics), e.g. "*Focus*" or "*.fit".
  A leading '/' anchors the pattern at the source root.
  A trailing '/' matches directories only.
An excluded directory is pruned: nothing under it is copied, even if a later
include rule would match it. That is also rsync's behaviour.
"""

import re
from dataclasses import dataclass, field


def _glob_re(pat):
    i, out = 0, []
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pat.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                body = pat[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


@dataclass
class Rule:
    include: bool
    pattern: str
    dir_only: bool = False
    regex: re.Pattern = None

    @classmethod
    def parse(cls, text):
        text = text.strip()
        if len(text) < 3 or text[0] not in "+-" or text[1] != " ":
            raise ValueError(f"filter rule must look like '+ PATTERN' or '- PATTERN': {text!r}")
        include, pat = text[0] == "+", text[2:].strip()
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        if pat.startswith("/"):
            rx = "^" + _glob_re(pat[1:]) + "$"
        elif "/" in pat:
            # rsync: a pattern with a slash (not leading) matches the end of the path
            # at a component boundary
            rx = "(?:^|/)" + _glob_re(pat) + "$"
        else:
            rx = "(?:^|/)" + _glob_re(pat) + "$"
        return cls(include, pat, dir_only, re.compile(rx))

    def matches(self, rel, is_dir):
        if self.dir_only and not is_dir:
            return False
        return self.regex.search(rel) is not None


@dataclass
class Filter:
    rules: list = field(default_factory=list)

    @classmethod
    def build(cls, filter=(), include=(), exclude=()):
        """Ordered `filter` rules first, then `include` (+), then `exclude` (-)."""
        rules = [Rule.parse(r) for r in filter]
        rules += [Rule.parse("+ " + p) for p in include]
        rules += [Rule.parse("- " + p) for p in exclude]
        return cls(rules)

    def __bool__(self):
        return bool(self.rules)

    def allows(self, rel, is_dir=False):
        for r in self.rules:
            if r.matches(rel, is_dir):
                return r.include
        return True

    def apply(self, entries, prefix=""):
        """Filter walk entries (in walk order, parents before children) whose `rel`
        starts with `prefix`. The path matched is rel minus prefix. Excluded
        directories prune their subtree."""
        pruned = []
        out = []
        for e in entries:
            rel = e["rel"]
            sub = rel[len(prefix):].lstrip("/") if prefix and rel.startswith(prefix) else rel
            if not sub:  # the source root itself
                out.append(e)
                continue
            if any(rel.startswith(p) for p in pruned):
                continue
            is_dir = e["type"] == "d"
            if self.allows(sub, is_dir):
                out.append(e)
            elif is_dir:
                pruned.append(rel + "/")
        return out
