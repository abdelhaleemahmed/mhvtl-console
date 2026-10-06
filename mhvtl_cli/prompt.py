"""Asking questions. The only file in the CLI that reads from stdin.

`mhvtl library create --interactive` is a form, not a brain: the options come
from services/profiles, the defaults from the same table the web's setup page
embeds, and the check from services/libraries/validation. What is left over is
the asking - print a question, read a line, refuse nonsense until an answer
arrives that can be used. That is this module, and nothing in it knows what a
library is.

WHY THE PROMPTS GO TO STDERR
----------------------------
output.py's rule is data on stdout and everything else on stderr. The one
thing an interactive create writes to stdout is the device.conf preview, so
`--interactive > my.conf` has to leave the questions on the terminal and put
the configuration in the file. input()'s own prompt argument writes to stdout,
which is why every question here is printed first and input() is then called
with no argument.

WHY IT IS A CLASS
-----------------
So that a test can answer it. ``Prompt(answers=[...])`` replaces the reader
and the writer, which is how test_interactive.py drives the whole loop with no
terminal anywhere. The alternative - patching builtins.input - replaces a
global for every other test in the same run.

NOT A TERMINAL IS A REFUSAL, NOT A HANG
---------------------------------------
require_terminal() is asked before the first question. A cron entry that runs
`library create --interactive` by mistake has to fail at once rather than wait
for an answer that will never come: the discipline colour.enabled() applies to
stdout, applied to stdin.
"""
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple


class Interrupted(Exception):
    """The operator ended it - Ctrl-C, Ctrl-D, or answers that ran out.

    Raised rather than returned so that it unwinds the whole question
    sequence from wherever it happens. The caller writes nothing: an
    interactive create holds everything in a dict until the last answer, which
    is what makes quitting free.
    """


def require_terminal(stream=None) -> bool:
    """Whether questions can be asked at all."""
    stream = stream or sys.stdin
    return hasattr(stream, 'isatty') and stream.isatty()


class Prompt:
    """Questions, and the answers to them.

    ``answers`` makes it scripted: each question takes the next one in the
    list, and running out raises Interrupted - so a test that under-answers
    fails as an unfinished conversation rather than blocking on a read.
    """

    def __init__(self, answers: Sequence[str] = None, out=None):
        self._answers = None if answers is None else list(answers)
        self._out = out if out is not None else sys.stderr
        #: Every question asked, in order, with what came back. The drift test
        #: reads this: the order of the questions is the order the data
        #: narrows, and it has to match the web form's.
        self.asked: List[Dict[str, Any]] = []

    # -- saying things ----------------------------------------------------

    def say(self, text: str = '') -> None:
        print(text, file=self._out)

    def _read(self) -> str:
        if self._answers is not None:
            if not self._answers:
                raise Interrupted('no answer was given')
            return self._answers.pop(0)
        try:
            return input()
        except (EOFError, KeyboardInterrupt):
            # A bare Ctrl-C would print a traceback over the half-asked
            # question. This leaves the terminal as it found it.
            print(file=self._out)
            raise Interrupted('nothing was written')

    def _answer(self, question: str, options, default) -> str:
        suffix = f' [{default}]' if default not in (None, '') else ''
        print(f'{question}{suffix}: ', end='', file=self._out, flush=True)
        given = self._read().strip()
        self.asked.append({'question': question, 'options': list(options or []),
                           'default': default, 'answer': given})
        return given

    # -- asking things ----------------------------------------------------

    def ask(self, question: str, default: str = None) -> str:
        """A line of text. Empty takes the default, if there is one."""
        while True:
            given = self._answer(question, None, default)
            if given:
                return given
            if default not in (None, ''):
                return str(default)
            self.say('  An answer is needed.')

    def ask_int(self, question: str, default: int = None, *,
                minimum: int = None, maximum: int = None) -> int:
        """A whole number in range, asked again until it is one.

        The limits are the service's - a library model's max_drives and
        max_slots come from profiles/personalities, capped by what this host
        has left (libraries/setup_form) - so the refusal can say what the
        number has to be rather than only that it was wrong.
        """
        while True:
            given = self._answer(question, None, default)
            # Recorded with the question, not only applied to the answer: the
            # drift test compares what the terminal offers against what the
            # web form offers, and a cap is part of that. The form capped the
            # drive count by the host's free SCSI targets long before the
            # loop did.
            self.asked[-1].update(minimum=minimum, maximum=maximum)
            if not given and default is not None:
                return int(default)
            try:
                value = int(given)
            except ValueError:
                self.say(f'  {given!r} is not a whole number.')
                continue
            if minimum is not None and value < minimum:
                self.say(f'  {value} is below the minimum of {minimum}.')
                continue
            if maximum is not None and value > maximum:
                self.say(f'  {value} is more than the maximum of {maximum}.')
                continue
            return value

    def choose(self, question: str, options: Sequence[str],
               default: str = None) -> str:
        """One of ``options``, by number or by name.

        The list is printed, which is the teaching: the options are what the
        vendor's catalogue holds for the choices already made, so a drive the
        chosen library model cannot take is never on it. Nothing a caller
        offers here is checked against anything - narrowing happens before the
        question, not after the answer.
        """
        options = list(options)
        if not options:
            raise Interrupted(f'there is nothing to choose for {question!r}')
        if len(options) == 1:
            self.say(f'{question}: {options[0]} (the only one)')
            self.asked.append({'question': question, 'options': options,
                               'default': options[0], 'answer': options[0]})
            return options[0]

        width = len(str(len(options)))
        for number, option in enumerate(options, start=1):
            mark = ' <- default' if option == default else ''
            self.say(f'  {str(number).rjust(width)}. {option}{mark}')
        while True:
            given = self._answer(question, options, default)
            if not given and default is not None:
                return default
            if given.isdigit() and 1 <= int(given) <= len(options):
                return options[int(given) - 1]
            matched = [option for option in options
                       if option.lower() == given.lower()]
            if matched:
                return matched[0]
            self.say(f'  {given!r} is not one of them. '
                     f'Type a number from 1 to {len(options)}, or the name.')

    def confirm(self, question: str, default: bool = False) -> bool:
        """Yes or no, with the default shown as the capital letter."""
        while True:
            given = self._answer(question, ('y', 'n'),
                                 'Y/n' if default else 'y/N').lower()
            if not given:
                return default
            if given in ('y', 'yes'):
                return True
            if given in ('n', 'no'):
                return False
            self.say('  Answer y or n.')

    def menu(self, question: str, choices: Sequence[Tuple[str, str]],
             default: str = None) -> str:
        """One key from ``choices``, given as (key, what it does) pairs."""
        keys = [key for key, _ in choices]
        self.say()
        for key, label in choices:
            self.say(f'  {key}  {label}')
        while True:
            given = self._answer(question, keys, default).lower()
            if not given and default is not None:
                return default
            if given in keys:
                return given
            # Typing the whole word is the commoner slip than typing a wrong
            # letter: 'create' for 'c', 'quit' for 'q'.
            started = [key for key, label in choices
                       if given and label.lower().startswith(given)]
            if len(started) == 1:
                return started[0]
            self.say(f'  Choose one of: {", ".join(keys)}')
