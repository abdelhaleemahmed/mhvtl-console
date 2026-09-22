"""Every `mhvtl` command shown in the documentation is a real command.

Documentation rots by being right when it was written. A command that was
renamed, or one invented while writing a page, reads exactly like a working
one - and the reader finds out by typing it. The parsers know the truth, so
they are asked.

This checks the noun and the verb. It does not run anything: nothing here
touches a library.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

GUI = Path(__file__).resolve().parents[3]
DOCS = GUI / 'docs'

#: `mhvtl <noun> <verb>` wherever it appears - in a command line, in prose,
#: in a table. The prompt and sudo are optional.
#:
#: Not preceded by a slash or a word character: `/etc/mhvtl` and
#: `/opt/mhvtl` are paths, and `find /etc/mhvtl -maxdepth 1` is not a command
#: this program has. Spaces and tabs only, never a newline: the line after
#: `home directory  /opt/mhvtl` is not its argument.
#: Only where it is written as a command: after a prompt, after sudo, or in
#: ``literal text``. "mhvtl writes it as type-length-value" is prose about the
#: program, and "(mhvtl services)" is a label in a diagram.
#: A noun starts with a letter: `mhvtl -V` is an option, and the page that
#: documents it as *not* a command should not be read as using one.
COMMAND = re.compile(r'(?:\$ |# |sudo |``)mhvtl[ \t]+(?:--\S+[ \t]+)*'
                     r'([a-z][a-z-]*)(?:[ \t]+([a-z][a-z-]*))?')

#: Words that follow `mhvtl` in prose or in sample output without being a
#: noun: "mhvtl loaded" is a line of `status system` output.
NOT_A_NOUN = {'gui', 'command', 'commands', 'reads', 'says', 'and', 'is',
              'are', 'the', 'to', 'on', 'from', 'does', 'it', 'loaded'}

#: Pages that document commands which do not exist yet, on purpose.
PLANS = {'sphinx/guides/plan-cli-hardware.rst', 'sphinx/guides/phase-two.rst'}


def commands_in_the_docs():
    """(noun, verb, page) for every command the documentation shows."""
    for page in sorted(DOCS.rglob('*.rst')):
        if '_build' in page.parts:
            continue
        relative = page.relative_to(DOCS)
        if str(relative) in PLANS:
            continue
        for noun, verb in COMMAND.findall(page.read_text()):
            yield noun, verb or '', relative


def the_real_commands():
    """{noun: {verbs}} from the argparse parsers themselves."""
    import argparse

    from mhvtl_cli.main import build_parser

    real = {}
    for action in build_parser()._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for noun, parser in action.choices.items():
            verbs = set()
            for sub in parser._actions:
                if isinstance(sub, argparse._SubParsersAction):
                    verbs |= set(sub.choices)
            real[noun] = verbs
    return real


class DocumentedCommandTests(SimpleTestCase):

    def test_every_documented_command_exists(self):
        real = the_real_commands()
        self.assertTrue(real, 'no commands found in the parsers')

        wrong = []
        for noun, verb, page in commands_in_the_docs():
            if noun in NOT_A_NOUN:
                continue
            if noun not in real:
                wrong.append(f'{page}: `mhvtl {noun}` is not a command')
            elif verb and real[noun] and verb not in real[noun]:
                wrong.append(f'{page}: `mhvtl {noun} {verb}` - {noun} has no '
                             f'verb {verb}')
        self.assertEqual(wrong, [], '\n'.join(wrong))

    def test_the_check_would_notice_a_wrong_one(self):
        """A test that cannot fail protects nothing."""
        real = the_real_commands()
        self.assertNotIn('librarry', real)
        self.assertNotIn('destroy', real.get('library', set()))
