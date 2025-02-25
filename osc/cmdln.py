# Copyright (c) 2002-2005 ActiveState Corp.
# License: MIT (see LICENSE.txt for license details)
# Author:  Trent Mick (TrentM@ActiveState.com)
# Home:    http://trentm.com/projects/cmdln/

from __future__ import print_function

"""An improvement on Python's standard cmd.py module.

As with cmd.py, this module provides "a simple framework for writing
line-oriented command intepreters."  This module provides a 'RawCmdln'
class that fixes some design flaws in cmd.Cmd, making it more scalable
and nicer to use for good 'cvs'- or 'svn'-style command line interfaces
or simple shells.  And it provides a 'Cmdln' class that add
optparse-based option processing. Basically you use it like this:

    import cmdln

    class MySVN(cmdln.Cmdln):
        name = "svn"

        @cmdln.alias('stat', 'st')
        @cmdln.option('-v', '--verbose', action='store_true'
                      help='print verbose information')
        def do_status(self, subcmd, opts, *paths):
            print "handle 'svn status' command"

        #...

    if __name__ == "__main__":
        shell = MySVN()
        retval = shell.main()
        sys.exit(retval)

See the README.txt or <http://trentm.com/projects/cmdln/> for more
details.
"""

__revision__ = "$Id: cmdln.py 1666 2007-05-09 03:13:03Z trentm $"
__version_info__ = (1, 0, 0)
__version__ = '.'.join(map(str, __version_info__))

import os
import re
import cmd
import optparse
import sys
import time
from pprint import pprint
from datetime import datetime

# this is python 2.x style
def introspect_handler_2(handler):
    # Extract the introspection bits we need.
    func = handler.im_func
    if func.func_defaults:
        func_defaults = func.func_defaults
    else:
        func_defaults = []
    return \
        func_defaults,   \
        func.func_code.co_argcount, \
        func.func_code.co_varnames, \
        func.func_code.co_flags,    \
        func

def introspect_handler_3(handler):
    defaults = handler.__defaults__
    if not defaults:
        defaults = []
    else:
        defaults = list(handler.__defaults__)
    return \
        defaults,   \
        handler.__code__.co_argcount, \
        handler.__code__.co_varnames, \
        handler.__code__.co_flags,    \
        handler.__func__

if sys.version_info[0] == 2:
    introspect_handler = introspect_handler_2
    bytes = lambda x, *args: x
else:
    introspect_handler = introspect_handler_3


#---- globals

LOOP_ALWAYS, LOOP_NEVER, LOOP_IF_EMPTY = range(3)

# An unspecified optional argument when None is a meaningful value.
_NOT_SPECIFIED = ("Not", "Specified")

# Pattern to match a TypeError message from a call that
# failed because of incorrect number of arguments (see
# Python/getargs.c).
_INCORRECT_NUM_ARGS_RE = re.compile(
    r"(takes [\w ]+ )(\d+)( arguments? \()(\d+)( given\))")

_INCORRECT_NUM_ARGS_RE_PY3 = re.compile(
    r"(missing\s+\d+.*)")

# Static bits of man page
MAN_HEADER = r""".TH %(ucname)s "1" "%(date)s" "%(name)s %(version)s" "User Commands"
.SH NAME
%(name)s \- Program to do useful things.
.SH SYNOPSIS
.B %(name)s
[\fIGLOBALOPTS\fR] \fISUBCOMMAND \fR[\fIOPTS\fR] [\fIARGS\fR...]
.br
.B %(name)s
\fIhelp SUBCOMMAND\fR
.SH DESCRIPTION
"""
MAN_COMMANDS_HEADER = r"""
.SS COMMANDS
"""
MAN_OPTIONS_HEADER = r"""
.SS GLOBAL OPTIONS
"""
MAN_FOOTER = r"""
.SH AUTHOR
This man page is automatically generated.
"""

#---- exceptions

class CmdlnError(Exception):
    """A cmdln.py usage error."""
    def __init__(self, msg):
        self.msg = msg
    def __str__(self):
        return self.msg

class CmdlnUserError(Exception):
    """An error by a user of a cmdln-based tool/shell."""
    pass



#---- public methods and classes

def alias(*aliases):
    """Decorator to add aliases for Cmdln.do_* command handlers.

    Example:
        class MyShell(cmdln.Cmdln):
            @cmdln.alias("!", "sh")
            def do_shell(self, argv):
                #...implement 'shell' command
    """
    def decorate(f):
        if not hasattr(f, "aliases"):
            f.aliases = []
        f.aliases += aliases
        return f
    return decorate

MAN_REPLACES = [
    (re.compile(r'(^|[ \t\[\'\|])--([^/ \t/,-]*)-([^/ \t/,-]*)-([^/ \t/,-]*)-([^/ \t/,-]*)-([^/ \t/,\|-]*)(?=$|[ \t=\]\'/,\|])'), r'\1\-\-\2\-\3\-\4\-\5\-\6'),
    (re.compile(r'(^|[ \t\[\'\|])--([^/ \t/,-]*)-([^/ \t/,-]*)-([^/ \t/,-]*)-([^/ \t/,\|-]*)(?=$|[ \t=\]\'/,\|])'), r'\1\-\-\2\-\3\-\4\-\5'),
    (re.compile(r'(^|[ \t\[\'\|])--([^/ \t/,-]*)-([^/ \t/,-]*)-([^/ \t/,\|-]*)(?=$|[ \t=\]\'/,\|])'), r'\1\-\-\2\-\3\-\4'),
    (re.compile(r'(^|[ \t\[\'\|])-([^/ \t/,-]*)-([^/ \t/,-]*)-([^/ \t/,\|-]*)(?=$|[ \t=\]\'/,\|])'), r'\1\-\2\-\3\-\4'),
    (re.compile(r'(^|[ \t\[\'\|])--([^/ \t/,-]*)-([^/ \t/,\|-]*)(?=$|[ \t=\]\'/,\|])'), r'\1\-\-\2\-\3'),
    (re.compile(r'(^|[ \t\[\'\|])-([^/ \t/,-]*)-([^/ \t/,\|-]*)(?=$|[ \t=\]\'/,\|])'), r'\1\-\2\-\3'),
    (re.compile(r'(^|[ \t\[\'\|])--([^/ \t/,\|-]*)(?=$|[ \t=\]\'/,\|])'), r'\1\-\-\2'),
    (re.compile(r'(^|[ \t\[\'\|])-([^/ \t/,\|-]*)(?=$|[ \t=\]\'/,\|])'), r'\1\-\2'),
    (re.compile(r"^'"), r" '"),
    ]

def man_escape(text):
    '''
    Escapes text to be included in man page.

    For now it only escapes dashes in command line options.
    '''
    for repl in MAN_REPLACES:
        text = repl[0].sub(repl[1], text)
    return text

class RawCmdln(cmd.Cmd):
    """An improved (on cmd.Cmd) framework for building multi-subcommand
    scripts (think "svn" & "cvs") and simple shells (think "pdb" and
    "gdb").

    A simple example:

        import cmdln

        class MySVN(cmdln.RawCmdln):
            name = "svn"

            @cmdln.aliases('stat', 'st')
            def do_status(self, argv):
                print "handle 'svn status' command"

        if __name__ == "__main__":
            shell = MySVN()
            retval = shell.main()
            sys.exit(retval)

    See <http://trentm.com/projects/cmdln> for more information.
    """
    name = None      # if unset, defaults basename(sys.argv[0])
    prompt = None    # if unset, defaults to self.name+"> "
    version = None   # if set, default top-level options include --version

    # Default messages for some 'help' command error cases.
    # They are interpolated with one arg: the command.
    nohelp = "no help on '%s'"
    unknowncmd = "unknown command: '%s'"

    helpindent = '' # string with which to indent help output

    # Default man page parts, please change them in subclass
    man_header = MAN_HEADER
    man_commands_header = MAN_COMMANDS_HEADER
    man_options_header = MAN_OPTIONS_HEADER
    man_footer = MAN_FOOTER

    def __init__(self, completekey='tab',
                 stdin=None, stdout=None, stderr=None):
        """Cmdln(completekey='tab', stdin=None, stdout=None, stderr=None)

        The optional argument 'completekey' is the readline name of a
        completion key; it defaults to the Tab key. If completekey is
        not None and the readline module is available, command completion
        is done automatically.

        The optional arguments 'stdin', 'stdout' and 'stderr' specify
        alternate input, output and error output file objects; if not
        specified, sys.* are used.

        If 'stdout' but not 'stderr' is specified, stdout is used for
        error output. This is to provide least surprise for users used
        to only the 'stdin' and 'stdout' options with cmd.Cmd.
        """
        if self.name is None:
            self.name = os.path.basename(sys.argv[0])
        if self.prompt is None:
            self.prompt = self.name+"> "
        self._name_str = self._str(self.name)
        self._prompt_str = self._str(self.prompt)
        if stdin is not None:
            self.stdin = stdin
        else:
            self.stdin = sys.stdin
        if stdout is not None:
            self.stdout = stdout
        else:
            self.stdout = sys.stdout
        if stderr is not None:
            self.stderr = stderr
        elif stdout is not None:
            self.stderr = stdout
        else:
            self.stderr = sys.stderr
        self.cmdqueue = []
        self.completekey = completekey
        self.cmdlooping = False

    def get_optparser(self):
        """Hook for subclasses to set the option parser for the
        top-level command/shell.

        This option parser is used retrieved and used by `.main()' to
        handle top-level options.

        The default implements a single '-h|--help' option. Sub-classes
        can return None to have no options at the top-level. Typically
        an instance of CmdlnOptionParser should be returned.
        """
        version = (self.version is not None
                    and "%s %s" % (self._name_str, self.version)
                    or None)
        return CmdlnOptionParser(self, version=version)

    def get_version(self):
        """
        Returns version of program. To be replaced in subclass.
        """
        return __version__

    def postoptparse(self):
        """Hook method executed just after `.main()' parses top-level
        options.

        When called `self.values' holds the results of the option parse.
        """
        pass

    def main(self, argv=None, loop=LOOP_NEVER):
        """A possible mainline handler for a script, like so:

            import cmdln
            class MyCmd(cmdln.Cmdln):
                name = "mycmd"
                ...

            if __name__ == "__main__":
                MyCmd().main()

        By default this will use sys.argv to issue a single command to
        'MyCmd', then exit. The 'loop' argument can be use to control
        interactive shell behaviour.

        Arguments:
            "argv" (optional, default sys.argv) is the command to run.
                It must be a sequence, where the first element is the
                command name and subsequent elements the args for that
                command.
            "loop" (optional, default LOOP_NEVER) is a constant
                indicating if a command loop should be started (i.e. an
                interactive shell). Valid values (constants on this module):
                    LOOP_ALWAYS     start loop and run "argv", if any
                    LOOP_NEVER      run "argv" (or .emptyline()) and exit
                    LOOP_IF_EMPTY   run "argv", if given, and exit;
                                    otherwise, start loop
        """
        if argv is None:
            argv = sys.argv
        else:
            argv = argv[:] # don't modify caller's list

        self.optparser = self.get_optparser()
        if self.optparser: # i.e. optparser=None means don't process for opts
            try:
                self.options, args = self.optparser.parse_args(argv[1:])
                # store args so we can use them in self.postoptparse()
                self.args = args
            except CmdlnUserError as ex:
                msg = "%s: %s\nTry '%s help' for info.\n"\
                      % (self.name, ex, self.name)
                self.stderr.write(self._str(msg))
                self.stderr.flush()
                return 1
            except StopOptionProcessing as ex:
                return 0
        else:
            self.options, args = None, argv[1:]
        self.postoptparse()

        if loop == LOOP_ALWAYS:
            if args:
                self.cmdqueue.append(args)
            return self.cmdloop()
        elif loop == LOOP_NEVER:
            if args:
                return self.cmd(args)
            else:
                return self.emptyline()
        elif loop == LOOP_IF_EMPTY:
            if args:
                return self.cmd(args)
            else:
                return self.cmdloop()

    def cmd(self, argv):
        """Run one command and exit.

            "argv" is the arglist for the command to run. argv[0] is the
                command to run. If argv is an empty list then the
                'emptyline' handler is run.

        Returns the return value from the command handler.
        """
        assert isinstance(argv, (list, tuple)), \
                "'argv' is not a sequence: %r" % argv
        retval = None
        try:
            argv = self.precmd(argv)
            retval = self.onecmd(argv)
            self.postcmd(argv)
        except:
            if not self.cmdexc(argv):
                raise
            retval = 1
        return retval

    def _str(self, s):
        """Safely convert the given str/unicode to a string for printing."""
        try:
            return str(s)
        except UnicodeError:
            #XXX What is the proper encoding to use here? 'utf-8' seems
            #    to work better than "getdefaultencoding" (usually
            #    'ascii'), on OS X at least.
            #return s.encode(sys.getdefaultencoding(), "replace")
            return s.encode("utf-8", "replace")

    def cmdloop(self, intro=None):
        """Repeatedly issue a prompt, accept input, parse into an argv, and
        dispatch (via .precmd(), .onecmd() and .postcmd()), passing them
        the argv. In other words, start a shell.

            "intro" (optional) is a introductory message to print when
                starting the command loop. This overrides the class
                "intro" attribute, if any.
        """
        self.cmdlooping = True
        self.preloop()
        if self.use_rawinput and self.completekey:
            try:
                import readline
                self.old_completer = readline.get_completer()
                readline.set_completer(self.complete)
                readline.parse_and_bind(self.completekey+": complete")
            except ImportError:
                pass
        try:
            if intro is None:
                intro = self.intro
            if intro:
                intro_str = self._str(intro)
                self.stdout.write(intro_str+'\n')
            self.stop = False
            retval = None
            while not self.stop:
                if self.cmdqueue:
                    argv = self.cmdqueue.pop(0)
                    assert isinstance(argv, (list, tuple)), \
                            "item on 'cmdqueue' is not a sequence: %r" % argv
                else:
                    if self.use_rawinput:
                        try:
                            try:
                                #python 2.x
                                line = raw_input(self._prompt_str)
                            except NameError:
                                line = input(self._prompt_str)
                        except EOFError:
                            line = 'EOF'
                    else:
                        self.stdout.write(self._prompt_str)
                        self.stdout.flush()
                        line = self.stdin.readline()
                        if not len(line):
                            line = 'EOF'
                        else:
                            line = line[:-1] # chop '\n'
                    argv = line2argv(line)
                try:
                    argv = self.precmd(argv)
                    retval = self.onecmd(argv)
                    self.postcmd(argv)
                except:
                    if not self.cmdexc(argv):
                        raise
                    retval = 1
                self.lastretval = retval
            self.postloop()
        finally:
            if self.use_rawinput and self.completekey:
                try:
                    import readline
                    readline.set_completer(self.old_completer)
                except ImportError:
                    pass
        self.cmdlooping = False
        return retval

    def precmd(self, argv):
        """Hook method executed just before the command argv is
        interpreted, but after the input prompt is generated and issued.

            "argv" is the cmd to run.

        Returns an argv to run (i.e. this method can modify the command
        to run).
        """
        return argv

    def postcmd(self, argv):
        """Hook method executed just after a command dispatch is finished.

            "argv" is the command that was run.
        """
        pass

    def cmdexc(self, argv):
        """Called if an exception is raised in any of precmd(), onecmd(),
        or postcmd(). If True is returned, the exception is deemed to have
        been dealt with. Otherwise, the exception is re-raised.

        The default implementation handles CmdlnUserError's, which
        typically correspond to user error in calling commands (as
        opposed to programmer error in the design of the script using
        cmdln.py).
        """
        exc_type, exc, traceback = sys.exc_info()
        if isinstance(exc, CmdlnUserError):
            msg = "%s %s: %s\nTry '%s help %s' for info.\n"\
                  % (self.name, argv[0], exc, self.name, argv[0])
            self.stderr.write(self._str(msg))
            self.stderr.flush()
            return True

    def onecmd(self, argv):
        if not argv:
            return self.emptyline()
        self.lastcmd = argv
        cmdname = self._get_canonical_cmd_name(argv[0])
        if cmdname:
            handler = self._get_cmd_handler(cmdname)
            if handler:
                return self._dispatch_cmd(handler, argv)
        return self.default(argv)

    def _dispatch_cmd(self, handler, argv):
        return handler(argv)

    def default(self, argv):
        """Hook called to handle a command for which there is no handler.

            "argv" is the command and arguments to run.

        The default implementation writes and error message to stderr
        and returns an error exit status.

        Returns a numeric command exit status.
        """
        errmsg = self._str(self.unknowncmd % (argv[0],))
        if self.cmdlooping:
            self.stderr.write(errmsg+"\n")
        else:
            self.stderr.write("%s: %s\nTry '%s help' for info.\n"
                              % (self._name_str, errmsg, self._name_str))
        self.stderr.flush()
        return 1

    def parseline(self, line):
        # This is used by Cmd.complete (readline completer function) to
        # massage the current line buffer before completion processing.
        # We override to drop special '!' handling.
        line = line.strip()
        if not line:
            return None, None, line
        elif line[0] == '?':
            line = 'help ' + line[1:]
        i, n = 0, len(line)
        while i < n and line[i] in self.identchars:
            i = i+1
        cmd, arg = line[:i], line[i:].strip()
        return cmd, arg, line

    def helpdefault(self, cmd, known):
        """Hook called to handle help on a command for which there is no
        help handler.

            "cmd" is the command name on which help was requested.
            "known" is a boolean indicating if this command is known
                (i.e. if there is a handler for it).

        Returns a return code.
        """
        if known:
            msg = self._str(self.nohelp % (cmd,))
            if self.cmdlooping:
                self.stderr.write(msg + '\n')
            else:
                self.stderr.write("%s: %s\n" % (self.name, msg))
        else:
            msg = self.unknowncmd % (cmd,)
            if self.cmdlooping:
                self.stderr.write(msg + '\n')
            else:
                self.stderr.write("%s: %s\n"
                                  "Try '%s help' for info.\n"
                                  % (self.name, msg, self.name))
        self.stderr.flush()
        return 1


    def do_help(self, argv):
        """${cmd_name}: give detailed help on a specific sub-command

        usage:
            ${name} help [SUBCOMMAND]
        """
        if len(argv) > 1: # asking for help on a particular command
            doc = None
            cmdname = self._get_canonical_cmd_name(argv[1]) or argv[1]
            if not cmdname:
                return self.helpdefault(argv[1], False)
            else:
                helpfunc = getattr(self, "help_"+cmdname, None)
                if helpfunc:
                    doc = helpfunc()
                else:
                    handler = self._get_cmd_handler(cmdname)
                    if handler:
                        doc = handler.__doc__
                    if doc is None:
                        return self.helpdefault(argv[1], handler != None)
        else: # bare "help" command
            doc = self.__class__.__doc__  # try class docstring
            if doc is None:
                # Try to provide some reasonable useful default help.
                if self.cmdlooping:
                    prefix = ""
                else:
                    prefix = self.name+' '
                doc = """usage:
                    %sSUBCOMMAND [ARGS...]
                    %shelp [SUBCOMMAND]

                ${option_list}
                ${command_list}
                ${help_list}
                """ % (prefix, prefix)
            cmdname = None

        if doc: # *do* have help content, massage and print that
            doc = self._help_reindent(doc)
            doc = self._help_preprocess(doc, cmdname)
            doc = doc.rstrip() + '\n' # trim down trailing space
            self.stdout.write(self._str(doc))
            self.stdout.flush()
    do_help.aliases = ["?"]


    def do_man(self, argv):
        """${cmd_name}: generates a man page

        usage:
            ${name} man
        """
        mandate = datetime.utcfromtimestamp(int(os.environ.get('SOURCE_DATE_EPOCH', time.time())))
        self.stdout.write(
            self.man_header % {
                'date': mandate.strftime('%b %Y'),
                'version': self.get_version(),
                'name': self.name,
                'ucname': self.name.upper()
                }
            )

        self.stdout.write(self.man_commands_header)
        commands = self._help_get_command_list()
        for command, doc in commands:
            cmdname = command.split(' ')[0]
            text = self._help_preprocess(doc, cmdname)
            lines = []
            for line in text.splitlines(False):
                if line[:8] == ' ' * 8:
                    line = line[8:]
                lines.append(man_escape(line))

            self.stdout.write(
                '.TP\n\\fB%s\\fR\n%s\n' % (command, '\n'.join(lines)))

        self.stdout.write(self.man_options_header)
        self.stdout.write(
            man_escape(self._help_preprocess('${option_list}', None)))

        self.stdout.write(self.man_footer)

        self.stdout.flush()

    def _help_reindent(self, help, indent=None):
        """Hook to re-indent help strings before writing to stdout.

            "help" is the help content to re-indent
            "indent" is a string with which to indent each line of the
                help content after normalizing. If unspecified or None
                then the default is use: the 'self.helpindent' class
                attribute. By default this is the empty string, i.e.
                no indentation.

        By default, all common leading whitespace is removed and then
        the lot is indented by 'self.helpindent'. When calculating the
        common leading whitespace the first line is ignored -- hence
        help content for Conan can be written as follows and have the
        expected indentation:

            def do_crush(self, ...):
                '''${cmd_name}: crush your enemies, see them driven before you...

                c.f. Conan the Barbarian'''
        """
        if indent is None:
            indent = self.helpindent
        lines = help.splitlines(0)
        _dedentlines(lines, skip_first_line=True)
        lines = [(indent+line).rstrip() for line in lines]
        return '\n'.join(lines)

    def _help_preprocess(self, help, cmdname):
        """Hook to preprocess a help string before writing to stdout.

            "help" is the help string to process.
            "cmdname" is the canonical sub-command name for which help
                is being given, or None if the help is not specific to a
                command.

        By default the following template variables are interpolated in
        help content. (Note: these are similar to Python 2.4's
        string.Template interpolation but not quite.)

        ${name}
            The tool's/shell's name, i.e. 'self.name'.
        ${option_list}
            A formatted table of options for this shell/tool.
        ${command_list}
            A formatted table of available sub-commands.
        ${help_list}
            A formatted table of additional help topics (i.e. 'help_*'
            methods with no matching 'do_*' method).
        ${cmd_name}
            The name (and aliases) for this sub-command formatted as:
            "NAME (ALIAS1, ALIAS2, ...)".
        ${cmd_usage}
            A formatted usage block inferred from the command function
            signature.
        ${cmd_option_list}
            A formatted table of options for this sub-command. (This is
            only available for commands using the optparse integration,
            i.e.  using @cmdln.option decorators or manually setting the
            'optparser' attribute on the 'do_*' method.)

        Returns the processed help.
        """
        preprocessors = {
            "${name}":            self._help_preprocess_name,
            "${option_list}":     self._help_preprocess_option_list,
            "${command_list}":    self._help_preprocess_command_list,
            "${help_list}":       self._help_preprocess_help_list,
            "${cmd_name}":        self._help_preprocess_cmd_name,
            "${cmd_usage}":       self._help_preprocess_cmd_usage,
            "${cmd_option_list}": self._help_preprocess_cmd_option_list,
        }

        for marker, preprocessor in preprocessors.items():
            if marker in help:
                help = preprocessor(help, cmdname)
        return help

    def _help_preprocess_name(self, help, cmdname=None):
        return help.replace("${name}", self.name)

    def _help_preprocess_option_list(self, help, cmdname=None):
        marker = "${option_list}"
        indent, indent_width = _get_indent(marker, help)
        suffix = _get_trailing_whitespace(marker, help)

        if self.optparser:
            # Setup formatting options and format.
            # - Indentation of 4 is better than optparse default of 2.
            #   C.f. Damian Conway's discussion of this in Perl Best
            #   Practices.
            self.optparser.formatter.indent_increment = 4
            self.optparser.formatter.current_indent = indent_width
            block = self.optparser.format_option_help() + '\n'
        else:
            block = ""

        help_msg = help.replace(indent+marker+suffix, block, 1)
        return help_msg

    def _help_get_command_list(self):
        # Find any aliases for commands.
        token2canonical = self._get_canonical_map()
        aliases = {}
        for token, cmdname in token2canonical.items():
            if token == cmdname:
                continue
            aliases.setdefault(cmdname, []).append(token)

        # Get the list of (non-hidden) commands and their
        # documentation, if any.
        cmdnames = {} # use a dict to strip duplicates
        for attr in self.get_names():
            if attr.startswith("do_"):
                cmdnames[attr[3:]] = True
        linedata = []
        for cmdname in sorted(cmdnames.keys()):
            if aliases.get(cmdname):
                a = sorted(aliases[cmdname])
                cmdstr = "%s (%s)" % (cmdname, ", ".join(a))
            else:
                cmdstr = cmdname
            doc = None
            try:
                helpfunc = getattr(self, 'help_'+cmdname)
            except AttributeError:
                handler = self._get_cmd_handler(cmdname)
                if handler:
                    doc = handler.__doc__
            else:
                doc = helpfunc()

            # Strip "${cmd_name}: " from the start of a command's doc. Best
            # practice dictates that command help strings begin with this, but
            # it isn't at all wanted for the command list.
            to_strip = "${cmd_name}:"
            if doc and doc.lstrip().startswith(to_strip):
                #log.debug("stripping %r from start of %s's help string",
                #          to_strip, cmdname)
                doc = (doc.lstrip())[len(to_strip):].lstrip()
            if not getattr(self._get_cmd_handler(cmdname), "hidden", None):
                linedata.append( (cmdstr, doc) )

        return linedata

    def _help_preprocess_command_list(self, help, cmdname=None):
        marker = "${command_list}"
        indent, indent_width = _get_indent(marker, help)
        suffix = _get_trailing_whitespace(marker, help)

        linedata = self._help_get_command_list()

        if linedata:
            subindent = indent + ' '*4
            lines = _format_linedata(linedata, subindent, indent_width+4)
            block = indent + "commands:\n" \
                    + '\n'.join(lines) + "\n\n"
            help = help.replace(indent+marker+suffix, block, 1)
        return help

    def _help_preprocess_help_list(self, help, cmdname=None):
        marker = "${help_list}"
        indent, indent_width = _get_indent(marker, help)
        suffix = _get_trailing_whitespace(marker, help)

        # Determine the additional help topics, if any.
        helpnames = {}
        token2cmdname = self._get_canonical_map()
        for attr in self.get_names():
            if not attr.startswith("help_"):
                continue
            helpname = attr[5:]
            if helpname not in token2cmdname:
                helpnames[helpname] = True

        if helpnames:
            helpnames = sorted(helpnames.keys())
            linedata = [(self.name+" help "+n, "") for n in helpnames]

            subindent = indent + ' '*4
            lines = _format_linedata(linedata, subindent, indent_width+4)
            block = indent + "additional help topics:\n" \
                    + '\n'.join(lines) + "\n\n"
        else:
            block = ''
        help_msg = help.replace(indent+marker+suffix, block, 1)
        return help_msg

    def _help_preprocess_cmd_name(self, help, cmdname=None):
        marker = "${cmd_name}"
        handler = self._get_cmd_handler(cmdname)
        if not handler:
            raise CmdlnError("cannot preprocess '%s' into help string: "
                             "could not find command handler for %r"
                             % (marker, cmdname))
        s = cmdname
        if hasattr(handler, "aliases"):
            s += " (%s)" % (", ".join(handler.aliases))
        help_msg = help.replace(marker, s)
        return help_msg

    #TODO: this only makes sense as part of the Cmdln class.
    #      Add hooks to add help preprocessing template vars and put
    #      this one on that class.
    def _help_preprocess_cmd_usage(self, help, cmdname=None):
        marker = "${cmd_usage}"
        handler = self._get_cmd_handler(cmdname)
        if not handler:
            raise CmdlnError("cannot preprocess '%s' into help string: "
                             "could not find command handler for %r"
                             % (marker, cmdname))
        indent, indent_width = _get_indent(marker, help)
        suffix = _get_trailing_whitespace(marker, help)

        func_defaults, co_argcount, co_varnames, co_flags, _ = introspect_handler(handler)
        CO_FLAGS_ARGS = 4
        CO_FLAGS_KWARGS = 8

        # Adjust argcount for possible *args and **kwargs arguments.
        argcount = co_argcount
        if co_flags & CO_FLAGS_ARGS:
            argcount += 1
        if co_flags & CO_FLAGS_KWARGS:
            argcount += 1

        # Determine the usage string.
        usage = "%s %s" % (self.name, cmdname)
        if argcount <= 2:   # handler ::= do_FOO(self, argv)
            usage += " [ARGS...]"
        elif argcount >= 3: # handler ::= do_FOO(self, subcmd, opts, ...)
            argnames = list(co_varnames[3:argcount])
            tail = ""
            if co_flags & CO_FLAGS_KWARGS:
                name = argnames.pop(-1)
                import warnings
                # There is no generally accepted mechanism for passing
                # keyword arguments from the command line. Could
                # *perhaps* consider: arg=value arg2=value2 ...
                warnings.warn("argument '**%s' on '%s.%s' command "
                              "handler will never get values"
                              % (name, self.__class__.__name__,
                                 getattr(func, "__name__", getattr(func, "func_name"))))
            if co_flags & CO_FLAGS_ARGS:
                name = argnames.pop(-1)
                tail = "[%s...]" % name.upper()
            while func_defaults:
                func_defaults.pop(-1)
                name = argnames.pop(-1)
                tail = "[%s%s%s]" % (name.upper(), (tail and ' ' or ''), tail)
            while argnames:
                name = argnames.pop(-1)
                tail = "%s %s" % (name.upper(), tail)
            usage += ' ' + tail

        block_lines = [
            self.helpindent + "Usage:",
            self.helpindent + ' '*4 + usage
        ]
        block = '\n'.join(block_lines) + '\n\n'

        help_msg = help.replace(indent+marker+suffix, block, 1)
        return help_msg

    #TODO: this only makes sense as part of the Cmdln class.
    #      Add hooks to add help preprocessing template vars and put
    #      this one on that class.
    def _help_preprocess_cmd_option_list(self, help, cmdname=None):
        marker = "${cmd_option_list}"
        handler = self._get_cmd_handler(cmdname)
        if not handler:
            raise CmdlnError("cannot preprocess '%s' into help string: "
                             "could not find command handler for %r"
                             % (marker, cmdname))
        indent, indent_width = _get_indent(marker, help)
        suffix = _get_trailing_whitespace(marker, help)
        if hasattr(handler, "optparser"):
            # Setup formatting options and format.
            # - Indentation of 4 is better than optparse default of 2.
            #   C.f. Damian Conway's discussion of this in Perl Best
            #   Practices.
            handler.optparser.formatter.indent_increment = 4
            handler.optparser.formatter.current_indent = indent_width
            block = handler.optparser.format_option_help() + '\n'
        else:
            block = ""

        help_msg = help.replace(indent+marker+suffix, block, 1)
        return help_msg

    def _get_canonical_cmd_name(self, token):
        c_map = self._get_canonical_map()
        return c_map.get(token, None)

    def _get_canonical_map(self):
        """Return a mapping of available command names and aliases to
        their canonical command name.
        """
        cacheattr = "_token2canonical"
        if not hasattr(self, cacheattr):
            # Get the list of commands and their aliases, if any.
            token2canonical = {}
            cmd2funcname = {} # use a dict to strip duplicates
            for attr in self.get_names():
                if attr.startswith("do_"):
                    cmdname = attr[3:]
                elif attr.startswith("_do_"):
                    cmdname = attr[4:]
                else:
                    continue
                cmd2funcname[cmdname] = attr
                token2canonical[cmdname] = cmdname
            for cmdname, funcname in cmd2funcname.items(): # add aliases
                func = getattr(self, funcname)
                aliases = getattr(func, "aliases", [])
                for alias in aliases:
                    if alias in cmd2funcname:
                        import warnings
                        warnings.warn("'%s' alias for '%s' command conflicts "
                                      "with '%s' handler"
                                      % (alias, cmdname, cmd2funcname[alias]))
                        continue
                    token2canonical[alias] = cmdname
            setattr(self, cacheattr, token2canonical)
        return getattr(self, cacheattr)

    def _get_cmd_handler(self, cmdname):
        handler = None
        try:
            handler = getattr(self, 'do_' + cmdname)
        except AttributeError:
            try:
                # Private command handlers begin with "_do_".
                handler = getattr(self, '_do_' + cmdname)
            except AttributeError:
                pass
        return handler

    def _do_EOF(self, argv):
        # Default EOF handler
        # Note: an actual EOF is redirected to this command.
        #TODO: separate name for this. Currently it is available from
        #      command-line. Is that okay?
        self.stdout.write('\n')
        self.stdout.flush()
        self.stop = True

    def emptyline(self):
        # Different from cmd.Cmd: don't repeat the last command for an
        # emptyline.
        if self.cmdlooping:
            pass
        else:
            return self.do_help(["help"])


#---- optparse.py extension to fix (IMO) some deficiencies
#
# See the class _OptionParserEx docstring for details.
#

class StopOptionProcessing(Exception):
    """Indicate that option *and argument* processing should stop
    cleanly. This is not an error condition. It is similar in spirit to
    StopIteration. This is raised by _OptionParserEx's default "help"
    and "version" option actions and can be raised by custom option
    callbacks too.

    Hence the typical CmdlnOptionParser (a subclass of _OptionParserEx)
    usage is:

        parser = CmdlnOptionParser(mycmd)
        parser.add_option("-f", "--force", dest="force")
        ...
        try:
            opts, args = parser.parse_args()
        except StopOptionProcessing:
            # normal termination, "--help" was probably given
            sys.exit(0)
    """

class _OptionParserEx(optparse.OptionParser):
    """An optparse.OptionParser that uses exceptions instead of sys.exit.

    This class is an extension of optparse.OptionParser that differs
    as follows:
    - Correct (IMO) the default OptionParser error handling to never
      sys.exit(). Instead OptParseError exceptions are passed through.
    - Add the StopOptionProcessing exception (a la StopIteration) to
      indicate normal termination of option processing.
      See StopOptionProcessing's docstring for details.

    I'd also like to see the following in the core optparse.py, perhaps
    as a RawOptionParser which would serve as a base class for the more
    generally used OptionParser (that works as current):
    - Remove the implicit addition of the -h|--help and --version
      options. They can get in the way (e.g. if want '-?' and '-V' for
      these as well) and it is not hard to do:
        optparser.add_option("-h", "--help", action="help")
        optparser.add_option("--version", action="version")
      These are good practices, just not valid defaults if they can
      get in the way.
    """
    def error(self, msg):
        raise optparse.OptParseError(msg)

    def exit(self, status=0, msg=None):
        if status == 0:
            raise StopOptionProcessing(msg)
        else:
            #TODO: don't lose status info here
            raise optparse.OptParseError(msg)



#---- optparse.py-based option processing support

class CmdlnOptionParser(_OptionParserEx):
    """An optparse.OptionParser class more appropriate for top-level
    Cmdln options. For parsing of sub-command options, see
    SubCmdOptionParser.

    Changes:
    - disable_interspersed_args() by default, because a Cmdln instance
      has sub-commands which may themselves have options.
    - Redirect print_help() to the Cmdln.do_help() which is better
      equiped to handle the "help" action.
    - error() will raise a CmdlnUserError: OptionParse.error() is meant
      to be called for user errors. Raising a well-known error here can
      make error handling clearer.
    - Also see the changes in _OptionParserEx.
    """
    def __init__(self, cmdln, **kwargs):
        self.cmdln = cmdln
        kwargs["prog"] = self.cmdln.name
        _OptionParserEx.__init__(self, **kwargs)
        self.disable_interspersed_args()

    def print_help(self, file=None):
        self.cmdln.onecmd(["help"])

    def error(self, msg):
        raise CmdlnUserError(msg)


class SubCmdOptionParser(_OptionParserEx):
    def set_cmdln_info(self, cmdln, subcmd):
        """Called by Cmdln to pass relevant info about itself needed
        for print_help().
        """
        self.cmdln = cmdln
        self.subcmd = subcmd

    def print_help(self, file=None):
        self.cmdln.onecmd(["help", self.subcmd])

    def error(self, msg):
        raise CmdlnUserError(msg)


def option(*args, **kwargs):
    """Decorator to add an option to the optparser argument of a Cmdln
    subcommand.

    Example:
        class MyShell(cmdln.Cmdln):
            @cmdln.option("-f", "--force", help="force removal")
            def do_remove(self, subcmd, opts, *args):
                #...
    """
    #XXX Is there a possible optimization for many options to not have a
    #    large stack depth here?
    def decorate(f):
        if not hasattr(f, "optparser"):
            f.optparser = SubCmdOptionParser()
        f.optparser.add_option(*args, **kwargs)
        return f
    return decorate

def hide(*args):
    """For obsolete calls, hide them in help listings.

    Example:
        class MyShell(cmdln.Cmdln):
            @cmdln.hide()
            def do_shell(self, argv):
                #...implement 'shell' command
    """
    def decorate(f):
        f.hidden = 1
        return f
    return decorate


class Cmdln(RawCmdln):
    """An improved (on cmd.Cmd) framework for building multi-subcommand
    scripts (think "svn" & "cvs") and simple shells (think "pdb" and
    "gdb").

    A simple example:

        import cmdln

        class MySVN(cmdln.Cmdln):
            name = "svn"

            @cmdln.aliases('stat', 'st')
            @cmdln.option('-v', '--verbose', action='store_true'
                          help='print verbose information')
            def do_status(self, subcmd, opts, *paths):
                print "handle 'svn status' command"

            #...

        if __name__ == "__main__":
            shell = MySVN()
            retval = shell.main()
            sys.exit(retval)

    'Cmdln' extends 'RawCmdln' by providing optparse option processing
    integration.  See this class' _dispatch_cmd() docstring and
    <http://trentm.com/projects/cmdln> for more information.
    """
    def _dispatch_cmd(self, handler, argv):
        """Introspect sub-command handler signature to determine how to
        dispatch the command. The raw handler provided by the base
        'RawCmdln' class is still supported:

            def do_foo(self, argv):
                # 'argv' is the vector of command line args, argv[0] is
                # the command name itself (i.e. "foo" or an alias)
                pass

        In addition, if the handler has more than 2 arguments option
        processing is automatically done (using optparse):

            @cmdln.option('-v', '--verbose', action='store_true')
            def do_bar(self, subcmd, opts, *args):
                # subcmd = <"bar" or an alias>
                # opts = <an optparse.Values instance>
                if opts.verbose:
                    print "lots of debugging output..."
                # args = <tuple of arguments>
                for arg in args:
                    bar(arg)

        TODO: explain that "*args" can be other signatures as well.

        The `cmdln.option` decorator corresponds to an `add_option()`
        method call on an `optparse.OptionParser` instance.

        You can declare a specific number of arguments:

            @cmdln.option('-v', '--verbose', action='store_true')
            def do_bar2(self, subcmd, opts, bar_one, bar_two):
                #...

        and an appropriate error message will be raised/printed if the
        command is called with a different number of args.
        """
        co_argcount = introspect_handler(handler)[1]
        if co_argcount == 2:   # handler ::= do_foo(self, argv)
            return handler(argv)
        elif co_argcount >= 3: # handler ::= do_foo(self, subcmd, opts, ...)
            try:
                optparser = handler.optparser
            except AttributeError:
                optparser = introspect_handler(handler)[4].optparser = SubCmdOptionParser()
            assert isinstance(optparser, SubCmdOptionParser)
            optparser.set_cmdln_info(self, argv[0])
            try:
                opts, args = optparser.parse_args(argv[1:])
            except StopOptionProcessing:
                #TODO: this doesn't really fly for a replacement of
                #      optparse.py behaviour, does it?
                return 0 # Normal command termination

            try:
                return handler(argv[0], opts, *args)
            except TypeError as ex:
                # Some TypeError's are user errors:
                #   do_foo() takes at least 4 arguments (3 given)
                #   do_foo() takes at most 5 arguments (6 given)
                #   do_foo() takes exactly 5 arguments (6 given)
                # Raise CmdlnUserError for these with a suitably
                # massaged error message.
                tb = sys.exc_info()[2] # the traceback object
                if tb.tb_next is not None:
                    # If the traceback is more than one level deep, then the
                    # TypeError do *not* happen on the "handler(...)" call
                    # above. In that we don't want to handle it specially
                    # here: it would falsely mask deeper code errors.
                    raise
                msg = ex.args[0]
                match = _INCORRECT_NUM_ARGS_RE.search(msg)
                match_py3 = _INCORRECT_NUM_ARGS_RE_PY3.search(msg)
                if match:
                    msg = list(match.groups())
                    msg[1] = int(msg[1]) - 3
                    if msg[1] == 1:
                        msg[2] = msg[2].replace("arguments", "argument")
                    msg[3] = int(msg[3]) - 3
                    msg = ''.join(map(str, msg))
                    raise CmdlnUserError(msg)
                elif match_py3:
                    raise CmdlnUserError(match_py3.group(1))
                else:
                    raise
        else:
            raise CmdlnError("incorrect argcount for %s(): takes %d, must "
                             "take 2 for 'argv' signature or 3+ for 'opts' "
                             "signature" % (handler.__name__, co_argcount))



#---- internal support functions

def _format_linedata(linedata, indent, indent_width):
    """Format specific linedata into a pleasant layout.

        "linedata" is a list of 2-tuples of the form:
            (<item-display-string>, <item-docstring>)
        "indent" is a string to use for one level of indentation
        "indent_width" is a number of columns by which the
            formatted data will be indented when printed.

    The <item-display-string> column is held to 15 columns.
    """
    lines = []
    WIDTH = 78 - indent_width
    SPACING = 3
    MAX_NAME_WIDTH = 15

    NAME_WIDTH = min(max([len(s) for s, d in linedata]), MAX_NAME_WIDTH)
    DOC_WIDTH = WIDTH - NAME_WIDTH - SPACING
    for namestr, doc in linedata:
        line = indent + namestr
        if len(namestr) <= NAME_WIDTH:
            line += ' ' * (NAME_WIDTH + SPACING - len(namestr))
        else:
            lines.append(line)
            line = indent + ' ' * (NAME_WIDTH + SPACING)
        line += _summarize_doc(doc, DOC_WIDTH)
        lines.append(line.rstrip())
    return lines

def _summarize_doc(doc, length=60):
    r"""Parse out a short one line summary from the given doclines.

        "doc" is the doc string to summarize.
        "length" is the max length for the summary

    >>> _summarize_doc("this function does this")
    'this function does this'
    >>> _summarize_doc("this function does this", 10)
    'this fu...'
    >>> _summarize_doc("this function does this\nand that")
    'this function does this and that'
    >>> _summarize_doc("this function does this\n\nand that")
    'this function does this'
    """
    if doc is None:
        return ""
    assert length > 3, "length <= 3 is absurdly short for a doc summary"
    doclines = doc.strip().splitlines(0)
    if not doclines:
        return ""

    summlines = []
    for i, line in enumerate(doclines):
        stripped = line.strip()
        if not stripped:
            break
        summlines.append(stripped)
        if len(''.join(summlines)) >= length:
            break

    summary = ' '.join(summlines)
    if len(summary) > length:
        summary = summary[:length-3] + "..."
    return summary


def line2argv(line):
    r"""Parse the given line into an argument vector.

        "line" is the line of input to parse.

    This may get niggly when dealing with quoting and escaping. The
    current state of this parsing may not be completely thorough/correct
    in this respect.

    >>> from cmdln import line2argv
    >>> line2argv("foo")
    ['foo']
    >>> line2argv("foo bar")
    ['foo', 'bar']
    >>> line2argv("foo bar ")
    ['foo', 'bar']
    >>> line2argv(" foo bar")
    ['foo', 'bar']

    Quote handling:

    >>> line2argv("'foo bar'")
    ['foo bar']
    >>> line2argv('"foo bar"')
    ['foo bar']
    >>> line2argv(r'"foo\"bar"')
    ['foo"bar']
    >>> line2argv("'foo bar' spam")
    ['foo bar', 'spam']
    >>> line2argv("'foo 'bar spam")
    ['foo bar', 'spam']
    >>> line2argv("'foo")
    Traceback (most recent call last):
        ...
    ValueError: command line is not terminated: unfinished single-quoted segment
    >>> line2argv('"foo')
    Traceback (most recent call last):
        ...
    ValueError: command line is not terminated: unfinished double-quoted segment
    >>> line2argv('some\tsimple\ttests')
    ['some', 'simple', 'tests']
    >>> line2argv('a "more complex" test')
    ['a', 'more complex', 'test']
    >>> line2argv('a more="complex test of " quotes')
    ['a', 'more=complex test of ', 'quotes']
    >>> line2argv('a more" complex test of " quotes')
    ['a', 'more complex test of ', 'quotes']
    >>> line2argv('an "embedded \\"quote\\""')
    ['an', 'embedded "quote"']
    """
    import string
    line = line.strip()
    argv = []
    state = "default"
    arg = None  # the current argument being parsed
    i = -1
    while True:
        i += 1
        if i >= len(line):
            break
        ch = line[i]

        if ch == "\\": # escaped char always added to arg, regardless of state
            if arg is None:
                arg = ""
            i += 1
            arg += line[i]
            continue

        if state == "single-quoted":
            if ch == "'":
                state = "default"
            else:
                arg += ch
        elif state == "double-quoted":
            if ch == '"':
                state = "default"
            else:
                arg += ch
        elif state == "default":
            if ch == '"':
                if arg is None:
                    arg = ""
                state = "double-quoted"
            elif ch == "'":
                if arg is None:
                    arg = ""
                state = "single-quoted"
            elif ch in string.whitespace:
                if arg is not None:
                    argv.append(arg)
                arg = None
            else:
                if arg is None:
                    arg = ""
                arg += ch
    if arg is not None:
        argv.append(arg)
    if state != "default":
        raise ValueError("command line is not terminated: unfinished %s "
                         "segment" % state)
    return argv


def argv2line(argv):
    r"""Put together the given argument vector into a command line.

        "argv" is the argument vector to process.

    >>> from cmdln import argv2line
    >>> argv2line(['foo'])
    'foo'
    >>> argv2line(['foo', 'bar'])
    'foo bar'
    >>> argv2line(['foo', 'bar baz'])
    'foo "bar baz"'
    >>> argv2line(['foo"bar'])
    'foo"bar'
    >>> print argv2line(['foo" bar'])
    'foo" bar'
    >>> print argv2line(["foo' bar"])
    "foo' bar"
    >>> argv2line(["foo'bar"])
    "foo'bar"
    """
    escapedArgs = []
    for arg in argv:
        if ' ' in arg and '"' not in arg:
            arg = '"'+arg+'"'
        elif ' ' in arg and "'" not in arg:
            arg = "'"+arg+"'"
        elif ' ' in arg:
            arg = arg.replace('"', r'\"')
            arg = '"'+arg+'"'
        escapedArgs.append(arg)
    return ' '.join(escapedArgs)


# Recipe: dedent (0.1) in /Users/trentm/tm/recipes/cookbook
def _dedentlines(lines, tabsize=8, skip_first_line=False):
    """_dedentlines(lines, tabsize=8, skip_first_line=False) -> dedented lines

        "lines" is a list of lines to dedent.
        "tabsize" is the tab width to use for indent width calculations.
        "skip_first_line" is a boolean indicating if the first line should
            be skipped for calculating the indent width and for dedenting.
            This is sometimes useful for docstrings and similar.

    Same as dedent() except operates on a sequence of lines. Note: the
    lines list is modified **in-place**.
    """
    DEBUG = False
    if DEBUG:
        print("dedent: dedent(..., tabsize=%d, skip_first_line=%r)"\
              % (tabsize, skip_first_line))
    indents = []
    margin = None
    for i, line in enumerate(lines):
        if i == 0 and skip_first_line:
            continue
        indent = 0
        for ch in line:
            if ch == ' ':
                indent += 1
            elif ch == '\t':
                indent += tabsize - (indent % tabsize)
            elif ch in '\r\n':
                continue # skip all-whitespace lines
            else:
                break
        else:
            continue # skip all-whitespace lines
        if DEBUG:
            print("dedent: indent=%d: %r" % (indent, line))
        if margin is None:
            margin = indent
        else:
            margin = min(margin, indent)
    if DEBUG:
        print("dedent: margin=%r" % margin)

    if margin is not None and margin > 0:
        for i, line in enumerate(lines):
            if i == 0 and skip_first_line:
                continue
            removed = 0
            for j, ch in enumerate(line):
                if ch == ' ':
                    removed += 1
                elif ch == '\t':
                    removed += tabsize - (removed % tabsize)
                elif ch in '\r\n':
                    if DEBUG:
                        print("dedent: %r: EOL -> strip up to EOL" % line)
                    lines[i] = lines[i][j:]
                    break
                else:
                    raise ValueError("unexpected non-whitespace char %r in "
                                     "line %r while removing %d-space margin"
                                     % (ch, line, margin))
                if DEBUG:
                    print("dedent: %r: %r -> removed %d/%d"\
                          % (line, ch, removed, margin))
                if removed == margin:
                    lines[i] = lines[i][j+1:]
                    break
                elif removed > margin:
                    lines[i] = ' '*(removed-margin) + lines[i][j+1:]
                    break
    return lines

def _dedent(text, tabsize=8, skip_first_line=False):
    """_dedent(text, tabsize=8, skip_first_line=False) -> dedented text

        "text" is the text to dedent.
        "tabsize" is the tab width to use for indent width calculations.
        "skip_first_line" is a boolean indicating if the first line should
            be skipped for calculating the indent width and for dedenting.
            This is sometimes useful for docstrings and similar.

    textwrap.dedent(s), but don't expand tabs to spaces
    """
    lines = text.splitlines(1)
    _dedentlines(lines, tabsize=tabsize, skip_first_line=skip_first_line)
    return ''.join(lines)


def _get_indent(marker, s, tab_width=8):
    """_get_indent(marker, s, tab_width=8) ->
        (<indentation-of-'marker'>, <indentation-width>)"""
    # Figure out how much the marker is indented.
    INDENT_CHARS = tuple(' \t')
    start = s.index(marker)
    i = start
    while i > 0:
        if s[i-1] not in INDENT_CHARS:
            break
        i -= 1
    indent = s[i:start]
    indent_width = 0
    for ch in indent:
        if ch == ' ':
            indent_width += 1
        elif ch == '\t':
            indent_width += tab_width - (indent_width % tab_width)
    return indent, indent_width

def _get_trailing_whitespace(marker, s):
    """Return the whitespace content trailing the given 'marker' in string 's',
    up to and including a newline.
    """
    suffix = ''
    start = s.index(marker) + len(marker)
    i = start
    while i < len(s):
        if s[i] in ' \t':
            suffix += s[i]
        elif s[i] in '\r\n':
            suffix += s[i]
            if s[i] == '\r' and i+1 < len(s) and s[i+1] == '\n':
                suffix += s[i+1]
            break
        else:
            break
        i += 1
    return suffix


# vim: sw=4 et
