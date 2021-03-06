#!/usr/bin/env python2.7
#
# Copyright (c) 2012 Dominic van Berkel
# See LICENSE for details.

"""Shirk: an easily extensible IRC bot based on Twisted."""

# Standard library imports
import importlib
import json
import logging

# Twisted imports
from twisted.words.protocols import irc
from twisted.internet import reactor, protocol

# Project imports
from util import Event
import users


class Shirk(irc.IRCClient):
    """A simple modular IRC bot.

    Shirk provides a fairly thin layer of glue between twisted.w.p.i.IRCClient
    and the plugs, providing configurability and further abstraction of stuff
    users do that the bot can respond to.

    There's some support for live loading and reloading of plugs, a number of
    events that plugs can subscribe to and a User abstraction so plugs can
    mostly just shuffle nicknames around but have access to less transient
    information when necessary.

    """
    versionName = "Shirk"
    versionNum = "0.5"
    sourceURL = "https://github.com/barometz/shirk"
    # List of events that don't need any other information in the hook,
    # unlike .command and .raw which need other params specified.
    _simple_events = [Event.addressed, Event.chanmsg, Event.private,
        Event.userjoined, Event.usercreated, Event.userremoved]

    def load_plugs(self):
        """Load the plugs listed in config."""
        self.plugs = {}
        self.hooks = {Event.raw:        {},  # dictionary of
                      Event.command:    {}}  # 'command': set([plug, plug])
        for ev in self._simple_events:
            self.hooks[ev] = set()
        for plugname in self.config['plugs']:
            try:
                self.load_plug(plugname)
            except ImportError:
                self.log.exception('Failed to load plug %s.', plugname)

    def load_plug(self, plugname):
        """Load the plug identified by plugname.

        Loads and reloads the module to make sure we get any updated code,
        instantiates the plug and tells it to request event hooks.
        Raises ImportError if the module can't be found.

        """
        module = importlib.import_module('plugs.' + plugname)
        reload(module)
        plug = module.Plug(self, self.startingup)
        self.plugs[plugname] = plug
        plug.hook_events()

    def remove_plug(self, plugname):
        """Remove the plug identified by plugname.

        Removes the plug from the list of plugs and all events.  Raises
        KeyError if the plug wasn't in self.plugs.

        """
        plug = self.plugs[plugname]
        plug.cleanup()
        for cmd, callbacks in self.hooks[Event.command].iteritems():
            callbacks.discard(plug)
        for cmd, callbacks in self.hooks[Event.raw].iteritems():
            callbacks.discard(plug)
        for ev in self._simple_events:
            self.hooks[ev].discard(plug)
        del self.plugs[plugname]

    def shutdown(self, msg):
        """Shutdown, as it says on the tin.

        Tell all plugs to clean up, tell the factory that we're shutting down
        and then quit with the specified message.

        """
        for name, plug in self.plugs.iteritems():
            plug.cleanup()
        self.factory.shuttingdown = True
        self.quit(msg)

    def sendLine(self, line):
        """Sends a line to the other end of the connection.

        Overridden to make sure everything's encoded right, something
        upstream doesn't like unicode strings.

        """
        line = line.encode('utf-8')
        irc.IRCClient.sendLine(self, line)

    ## Twisted's callbacks
    # Things the bot does

    def connectionMade(self):
        """A connection with the server has been established.

        De facto init method.  Old-style classes are awesome, yo.

        """
        self.log = self.factory.log
        self.log.info('Connected to server')
        # Connected successfully, so reset the reconn delay
        self.factory.resetDelay()
        self.users = users.Users(self)
        self.nickname = self.config['nickname']
        self.password = self.config['password']
        self.cmd_prefix = self.config['cmd_prefix']
        self.realname = self.config['realname']
        self.username = self.config['username']
        self.startingup = True
        irc.IRCClient.connectionMade(self)

    def connectionLost(self, reason):
        """Called when the connection is shut down.

        This can happen for various reasons including network failure, a clean
        QUIT or a ctrl-c at the terminal.

        reason is typically an exception of some sort with information as to
        whether it was a clean disconnect.

        """
        self.log.info('Connection lost: %s' % (reason,))
        try:
            for name, plug in self.plugs.iteritems():
                plug.cleanup()
            del self.users
        except AttributeError:
            # this happens when the bot is shutdown before having connected
            # and signed on properly, no need to worry.
            pass
        irc.IRCClient.connectionLost(self, reason)

    def signedOn(self):
        """Called when bot has succesfully signed on to server."""
        self.log.info('Signed on')
        self.load_plugs()
        for chan in self.config['channels']:
            self.join(chan)
        self.lineRate = 0.3
        self.startingup = False

    def joined(self, channel):
        """Called when I finish joining a channel."""
        self.sendLine('WHO %s' % (channel,))

    # Things other users do

    def userJoined(self, user, channel):
        nickname, rest = user.split('!', 1)
        username, hostmask = rest.split('@', 1)
        self.users.user_joined(nickname, username, hostmask, channel)
        self.event_userjoined(nickname, channel)

    def userLeft(self, user, channel):
        self.users.user_left(user, channel)

    def userKicked(self, kickee, channel, kicker, message):
        self.users.user_left(kickee, channel)

    def userQuit(self, user, quitMessage):
        self.users.user_quit(user)

    def userRenamed(self, oldname, newname):
        self.users.user_nickchange(oldname, newname)

    def privmsg(self, user, target, msg):
        """The bot receives a PRIVMSG, either in channel or in PM"""
        user = user.split('!', 1)[0]
        msg = msg.strip()
        self.log.debug('%s: <%s> %s' % (target, user, msg))
        # Check to see if they're sending me a private message
        if target == self.nickname:
            self.event_private(user, msg, False)
        else:
            self.event_chanmsg(user, target, msg, False)
        if msg.startswith(self.cmd_prefix) and len(msg) > 1:
            argv = msg[len(self.cmd_prefix):].split()
            self.event_command(user, target, argv)
        elif msg.startswith(self.nickname):
            # +1 to account for : or , or whatever
            message = msg[len(self.nickname) + 1:].strip()
            self.event_addressed(user, target, message)

    def action(self, user, target, msg):
        """The bot sees someone perform a CTCP ACTION, or "/me"."""
        user = user.split('!', 1)[0]
        msg = msg.strip()
        self.log.debug('%s: * %s %s' % (target, user, msg))
        # Check to see if they're sending me a private message
        if target == self.nickname:
            self.event_private(user, msg, True)
        else:
            self.event_chanmsg(user, target, msg, True)

    # Lower-level callbacks

    def irc_JOIN(self, prefix, params):
        """Called when a user joins a channel."""
        nick = prefix.split('!', 1)[0]
        channel = params[-1]
        if nick == self.nickname:
            self.joined(channel)
        else:
            self.userJoined(prefix, channel)

    def irc_RPL_WHOREPLY(self, prefix, params):
        """Received a reply to a vanilla WHO command"""
        self.users.user_joined(params[5],  # nickname
                               params[2],  # username
                               params[3],  # hostmask
                               params[1])  # channel
        self.event_userjoined(params[5], params[1])

    def lineReceived(self, line):
        line = irc.lowDequote(line).decode(self.config['charset'], 'replace')
        try:
            prefix, command, params = irc.parsemsg(line)
            if command in irc.numeric_to_symbolic:
                parsedcmd = irc.numeric_to_symbolic[command]
            else:
                parsedcmd = command
            self.handleCommand(parsedcmd, prefix, params)
            if self._registered:
                self.event_raw(command, prefix, params)
        except irc.IRCBadMessage:
            self.badMessage(line, *sys.exc_info())

    ## Shirk's events that modules can register callbacks for

    def event_addressed(self, source, target, msg):
        """The bot is addressed directly by another user.

        As in "<tim> shirk: hi there".
        source: The nickname of whoever sent it.
        target: The channel.
        msg: The actual message.

        """
        for plug in self.hooks[Event.addressed]:
            plug.handle_addressed(source, target, msg)

    def event_chanmsg(self, source, channel, msg, action):
        """The bot is sent a message in a channel.

        source: The nickname of whoever sent it.
        channel: The channel.
        msg: The actual message.
        action: A bool indicating whether this was a CTCP ACTION ('/me')

        """
        for plug in self.hooks[Event.chanmsg]:
            plug.handle_chanmsg(source, channel, msg, action)

    def event_command(self, source, target, argv):
        """The bot receives a !command.

        source: The nickname of whoever sent it.
        target: The channel.
        argv: A list of the command and any arguments.

        """
        if argv[0] in self.hooks[Event.command]:
            # copying, otherwise any command that modifies the plug collection
            # raises an error "Set changed size during iteration"
            to_call = set(self.hooks[Event.command][argv[0]])
            for plug in to_call:
                plug.handle_command(source, target, argv)

    def event_private(self, source, msg, action):
        """The bot is sent a message in PM.

        source: The nickname of whoever sent it
        msg: The actual message
        action: A bool indicating whether this was a CTCP ACTION ('/me')

        """
        for plug in self.hooks[Event.private]:
            plug.handle_private(source, msg, action)

    def event_raw(self, command, prefix, params):
        """Pretty much any message triggers this event.

        Messy to explain, but an example is easy:
        "pratchett.freenode.net 330 shirks barometz nazgjunk :is logged in as"
        leads to:
        command: 330
        prefix: pratchett.freenode.net
        params: ['shirks', 'barometz', 'nazgjunk', 'is logged in as']

        """
        if command in self.hooks[Event.raw]:
            to_call = set(self.hooks[Event.raw][command])
            for plug in to_call:
                plug.handle_raw(command, prefix, params)

    def event_userjoined(self, nickname, channel):
        """A user has joined a channel, or the bot joined a channel.

        Is called when a new user joins a channel, and for all users in a
        channel that the bot just joined.

        """
        for plug in self.hooks[Event.userjoined]:
            plug.handle_userjoined(nickname, channel)

    def event_usercreated(self, user):
        """A new user has been introduced to user management.

        This is triggered from the Users instance whenever it creates a new
        User instance.

        """
        for plug in self.hooks[Event.usercreated]:
            plug.handle_usercreated(user)

    def event_userremoved(self, user):
        """A user has left the building.

        This is triggered from the Users instance whenever it removes a user
        from its list, generally because the user no longer shares a channel
        with the bot.

        """
        for plug in self.hooks[Event.userremoved]:
            plug.handle_userremoved(user)

    ## Things modules will want to use

    def add_command(self, cmd, plug):
        """Add a callback for a specific !commmand.

        cmd: The command that should trigger the callback, without the leading
            prefix (so 'command', not '!command')
        plug: The plug that wants to be notified.  See plugbase for a
            description of the arguments.

        """
        if cmd not in self.hooks[Event.command]:
            self.hooks[Event.command][cmd] = set()
        self.hooks[Event.command][cmd].add(plug)

    def add_callback(self, event, plug):
        """Add a callback for a given event.

        This method is for those events that do not have a more specific
        filter like the one for !commands.

        Params
        event: One of the constant attributes of util.Event.
        plug: The plug that's requesting to be poked in the event of an
            event.

        Returns true if the callback is now registered, false if the event
        doesn't exist.

        """
        if event not in self.hooks:
            return False
        else:
            self.hooks[event].add(plug)
            return True

    def add_raw(self, cmd, plug):
        """Add a callback for a raw IRC command.

        For instance, a plug might want to catch all PONG replies or some
        server-specific message.

        cmd: The command as it's sent over the line - not 'RPL_WHOREPLY' but
            '352'.
        plug: the plug that wants to be notified.

        """
        if cmd not in self.hooks[Event.raw]:
            self.hooks[Event.raw][cmd] = set()
        self.hooks[Event.raw][cmd].add(plug)


class ShirkFactory(protocol.ReconnectingClientFactory):
    """A factory for Shirk.

    A new protocol instance will be created each time we connect to the server.

    """
    def __init__(self, config, logger):
        self.shuttingdown = False
        self.config = config
        self.log = logger
        # self.noisy is used by ReconnClientFactory to enable logging, but as
        # that uses twisted.python.log we'll just do it ourselves, yes?
        self.noisy = False
        self.initialDelay = config['reconn_delay']
        self.delay = self.initialDelay
        self.maxRetries = config['reconn_tries']

    def buildProtocol(self, addr):
        p = Shirk()
        p.factory = self
        p.config = self.config
        return p

    def clientConnectionLost(self, connector, reason):
        """If we get disconnected, reconnect to server."""
        if self.shuttingdown:
            self.log.info('Shutting down')
            reactor.stop()
        else:
            self.log.info('Lost connection.')
            protocol.ReconnectingClientFactory.clientConnectionLost(
                self, connector, reason)
            self.log.info('Attempting reconnection in %d seconds.'
                % (self.delay,))

    def clientConnectionFailed(self, connector, reason):
        """Failed to connect to the server, so try to reconnect.

        Todo: write something nice to reconnect with increasing intervals.

        """
        self.log.info('Connection failed.')
        protocol.ReconnectingClientFactory.clientConnectionFailed(
            self, connector, reason)
        if self.maxRetries is not None and (self.retries > self.maxRetries):
            self.log.error('Abandoning reconnection after %d tries'
                % (self.retries,))
            reactor.stop()
        else:
            self.log.info('Attempting reconnection in %d seconds.'
                % (self.delay,))


if __name__ == '__main__':
    # set up default config dictionary
    config = {
        # The nickname to use
        'nickname': 'shirk',
        # Password to send while connecting.  Many servers pass this on to
        # Nickserv.
        'password': '',
        # The bot's "real" name
        'realname': 'Fedmahn',
        # Username, generally useless unless there's an identd server running.
        'username': 'shirk',
        # Debug level: 0 -> warnings, 1 -> info, 2 -> full debug
        'debug': 0,
        # Channels to join at first
        'channels': [],
        'server': 'chat.freenode.net',
        'port': 6667,
        # The plugs to load at startup.
        'plugs': ['Core', 'Auth'],
        # The prefix for !commands (or +commands, or @commands, or..)
        'cmd_prefix': '!',
        # Initial delay between reconnections when there's a connection
        # failure.
        'reconn_delay': 1,
        # Maximum reconnection retries
        'reconn_tries': 8,
        # charset used to decode messages
        'charset': 'utf-8'
    }
    config.update(json.load(open('conf.json')))
    
    loglevel = {0: logging.WARNING,
                1: logging.INFO,
                2: logging.DEBUG}[config['debug']]
    logger = logging.getLogger('shirk')
    logger.setLevel(loglevel)
    consolelog = logging.StreamHandler()
    consolelog.setLevel(logging.DEBUG)
    filelog = logging.FileHandler('shirk.log', encoding='utf-8')
    filelog.setLevel(logging.INFO)
    consolelog.setFormatter(logging.Formatter(
        fmt='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
        datefmt='%m/%d %H:%M:%S'))
    filelog.setFormatter(logging.Formatter(
        fmt='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
        datefmt='%Y-%m-%d/%H:%M:%S'))
    logger.addHandler(consolelog)
    logger.addHandler(filelog)

    # Create and connect the client factory
    f = ShirkFactory(config, logger)
    reactor.connectTCP(config['server'], config['port'], f)
    # Push the big red button
    reactor.run()
