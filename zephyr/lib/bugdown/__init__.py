import markdown
import logging
import traceback
import urlparse
import re

from zephyr.lib.avatar  import gravatar_hash
from zephyr.lib.bugdown import codehilite, fenced_code
from zephyr.lib.bugdown.fenced_code import FENCE_RE
from zephyr.lib.timeout import timeout

class Gravatar(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match):
        img = markdown.util.etree.Element('img')
        img.set('class', 'message_body_gravatar img-rounded')
        img.set('src', 'https://secure.gravatar.com/avatar/%s?d=identicon&s=30'
            % (gravatar_hash(match.group('email')),))
        return img

def fixup_link(link):
    """Set certain attributes we want on every link."""
    link.set('target', '_blank')
    link.set('title',  link.get('href'))

class AutoLink(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match):
        url = match.group('url')
        a = markdown.util.etree.Element('a')
        a.set('href', url)
        a.text = url
        fixup_link(a)
        return a

class UListProcessor(markdown.blockprocessors.OListProcessor):
    """ Process unordered list blocks.

        Based on markdown.blockprocessors.UListProcessor, but does not accept
        '+' or '-' as a bullet character."""

    TAG = 'ul'
    RE = re.compile(r'^[ ]{0,3}[*][ ]+(.*)')

class BugdownUListPreprocessor(markdown.preprocessors.Preprocessor):
    """ Allows unordered list blocks that come directly after a
        paragraph to be rendered as an unordered list

        Detects paragraphs that have a matching list item that comes
        directly after a line of text, and inserts a newline between
        to satisfy Markdown"""

    LI_RE = re.compile(r'^[ ]{0,3}[*][ ]+(.*)', re.MULTILINE)
    HANGING_ULIST_RE = re.compile(r'^.+\n([ ]{0,3}[*][ ]+.*)', re.MULTILINE)

    def run(self, lines):
        """ Insert a newline between a paragraph and ulist if missing """
        inserts = 0
        fence = None
        copy = lines[:]
        for i in xrange(len(lines) - 1):
            # Ignore anything that is inside a fenced code block
            m = FENCE_RE.match(lines[i])
            if not fence and m:
                fence = m.group('fence')
            elif fence and m and fence == m.group('fence'):
                fence = None

            # If we're not in a fenced block and we detect an upcoming list
            #  hanging off a paragraph, add a newline
            if not fence and lines[i] and \
                self.LI_RE.match(lines[i+1]) and not self.LI_RE.match(lines[i]):
                copy.insert(i+inserts+1, '')
                inserts += 1
        return copy

# Based on markdown.inlinepatterns.LinkPattern
class LinkPattern(markdown.inlinepatterns.Pattern):
    """ Return a link element from the given match. """
    def handleMatch(self, m):
        el = markdown.util.etree.Element("a")
        el.text = m.group(2)
        href = m.group(9)

        if href:
            if href[0] == "<":
                href = href[1:-1]
            el.set("href", self.sanitize_url(self.unescape(href.strip())))
        else:
            el.set("href", "")

        fixup_link(el)
        return el

    def sanitize_url(self, url):
        """
        Sanitize a url against xss attacks.
        See the docstring on markdown.inlinepatterns.LinkPattern.sanitize_url.
        """
        try:
            parts = urlparse.urlparse(url.replace(' ', '%20'))
            scheme, netloc, path, params, query, fragment = parts
        except ValueError:
            # Bad url - so bad it couldn't be parsed.
            return ''

        # Humbug modification: If scheme is not specified, assume http://
        # It's unlikely that users want relative links within humbughq.com.
        # We re-enter sanitize_url because netloc etc. need to be re-parsed.
        if not scheme:
            return self.sanitize_url('http://' + url)

        locless_schemes = ['', 'mailto', 'news']
        if netloc == '' and scheme not in locless_schemes:
            # This fails regardless of anything else.
            # Return immediately to save additional proccessing
            return ''

        for part in parts[2:]:
            if ":" in part:
                # Not a safe url
                return ''

        # Url passes all tests. Return url as-is.
        return urlparse.urlunparse(parts)

class Bugdown(markdown.Extension):
    def extendMarkdown(self, md, md_globals):
        del md.preprocessors['reference']

        for k in ('image_link', 'image_reference', 'automail',
                  'autolink', 'link', 'reference', 'short_reference',
                  'escape'):
            del md.inlinePatterns[k]

        for k in ('hashheader', 'setextheader', 'olist', 'ulist'):
            del md.parser.blockprocessors[k]

        md.parser.blockprocessors.add('ulist', UListProcessor(md.parser), '>hr')

        md.inlinePatterns.add('gravatar', Gravatar(r'!gravatar\((?P<email>[^)]*)\)'), '_begin')
        md.inlinePatterns.add('link', LinkPattern(markdown.inlinepatterns.LINK_RE, md), '>backtick')

        # A link starts at a word boundary, and ends at space or end-of-input.
        # But any trailing punctuation (other than /) is not included.
        # We accomplish this with a non-greedy match followed by a greedy
        # lookahead assertion.
        #
        # markdown.inlinepatterns.Pattern compiles this with re.UNICODE, which
        # is important because we're using \w.
        link_regex = r'\b(?P<url>https?://[^\s]+?)(?=[^\w/]*(\s|\Z))'
        md.inlinePatterns.add('autolink', AutoLink(link_regex), '>link')

        md.preprocessors.add('hanging_ulists',
                                 BugdownUListPreprocessor(md),
                                 "_begin")

_md_engine = markdown.Markdown(
    safe_mode     = 'escape',
    output_format = 'html',
    extensions    = ['nl2br',
        codehilite.makeExtension(configs=[
            ('force_linenos', False),
            ('guess_lang',    False)]),
        fenced_code.makeExtension(),
        Bugdown()])

# We want to log Markdown parser failures, but shouldn't log the actual input
# message for privacy reasons.  The compromise is to replace all alphanumeric
# characters with 'x'.
#
# We also use repr() to improve reproducibility, and to escape terminal control
# codes, which can do surprisingly nasty things.
_privacy_re = re.compile(r'\w', flags=re.UNICODE)
def _sanitize_for_log(md):
    return repr(_privacy_re.sub('x', md))

def convert(md):
    """Convert Markdown to HTML, with Humbug-specific settings and hacks."""

    # Reset the parser; otherwise it will get slower over time.
    _md_engine.reset()

    try:
        # Spend at most 5 seconds rendering.
        # Sometimes Python-Markdown is really slow; see
        # https://trac.humbughq.com/ticket/345
        html = timeout(5, _md_engine.convert, md)
    except:
        # FIXME: Do something more reasonable here!
        html = '<p>[Humbug note: Sorry, we could not understand the formatting of your message]</p>'
        logging.getLogger('').error('Exception in Markdown parser: %sInput (sanitized) was: %s'
            % (traceback.format_exc(), _sanitize_for_log(md)))

    return html
