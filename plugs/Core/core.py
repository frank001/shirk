# Copyright (c) 2012 Dominic van Berkel
# See LICENSE for details.

from plugs import plugbase
from util import Event

class CorePlug(plugbase.Plug):
    """Core stuff plug for Shirk.

    Tasks like "list all loaded plugs" and "what commands are available right
    now" go here.  This is separate from shirk.py because it's a lot cleaner
    that way, but at the same time this plug will depend a lot on Shirk's
    internals.

    """
    name = 'Core'
    commands = ['plugs', 'commands', 'raw', 'quit', 'reload']

    def cmd_commands(self, source, target, argv):
        """List registered commands."""
        response = ', '.join(self.core.hooks[Event.command].keys())
        self.respond(source, target, response)

    def cmd_plugs(self, source, target, argv):
        """List the loaded plugs"""
        response = ', '.join(self.core.plugs)
        self.respond(source, target, response)

    def cmd_quit(self, source, target, argv):
        """Disconnect and close."""
        if self.users[source].power >= 10:
            self.core.shutdown('Requested by ' + source)

    def cmd_raw(self, source, target, argv):
        """Send a raw message to the server."""
        if self.users[source].power >= 10:
            self.core.sendLine(' '.join(argv[1:]))

    def cmd_reload(self, source, target, argv):
        """Reload specified modules."""
        if self.users[source].power >= 10:
            for plugname in argv[1:]:
                # keep core safe in case this plug is being reloaded, which
                # clears self.core
                core = self.core
                try:
                    core.remove_plug(plugname)
                except KeyError:
                    self.log.warning('Tried to remove unknown plug %s.' % (plugname,))
                try:
                    core.load_plug(plugname)
                except ImportError:
                    self.respond(source, target, 'Failed to import %s.' % (plugname,))

