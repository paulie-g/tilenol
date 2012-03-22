from functools import partial
import sys
import subprocess
import os.path
import signal
import logging

from zorro.di import DependencyInjector, di, has_dependencies, dependency

from .xcb import Connection, Proto, Core, Keysyms, Rectangle
from .keyregistry import KeyRegistry
from .mouseregistry import MouseRegistry
from .ewmh import Ewmh
from .window import Window
from .events import EventDispatcher
from .commands import CommandDispatcher, EnvCommands
from .config import Config
from .groups import Group, GroupManager
from .screen import ScreenManager
from .classify import Classifier
from .theme import Theme


log = logging.getLogger(__name__)


def child_handler(sig, frame):
    while True:
        try:
            pid, result = os.waitpid(-1, os.WNOHANG)
            if pid is 0:
                break
        except OSError:
            break

def quit_handler(sig, frame):
    os.execv(sys.executable, [sys.executable] + sys.argv)

@has_dependencies
class Tilenol(object):

    xcore = dependency(Core, 'xcore')
    dispatcher = dependency(EventDispatcher, 'event-dispatcher')
    config = dependency(Config, 'config')
    commander = dependency(CommandDispatcher, 'commander')

    def __init__(self, options):
        pass
        # extract options needed

    def register_hotkeys(self, keys):
        for key, cmd in self.config.keys():
            keys.add_key(key, self.commander.callback(*cmd))
        keys.register_keys(self.root_window)

    def run(self):
        proto = Proto()
        proto.load_xml('xproto')
        proto.load_xml('xinerama')
        proto.load_xml('shm')
        self.conn = conn = Connection(proto)
        conn.connection()
        self.root_window = Window(conn.init_data['roots'][0]['root'])

        inj = DependencyInjector()
        inj['xcore'] = xcore = Core(conn)
        inj['keysyms'] = keysyms = Keysyms()
        keysyms.load_default()
        cfg = inj['config'] = inj.inject(Config())
        inj['theme'] = inj.inject(cfg.theme())
        inj['commander'] = cmd = inj.inject(CommandDispatcher())
        cmd['env'] = EnvCommands()
        if hasattr(xcore, 'xinerama'):
            info = xcore.xinerama.QueryScreens()['screen_info']
            screenman = inj['screen-manager'] = ScreenManager([
                Rectangle(scr['x_org'], scr['y_org'],
                    scr['width'], scr['height'])
                for scr in info])
        else:
            screenman = inj['screen-manager'] = ScreenManager([Rectangle(0, 0,
                xcore.root['width_in_pixels'],
                xcore.root['height_in_pixels'])])
        inj.inject(screenman)

        cmd['tilenol'] = self
        keys = KeyRegistry()
        inj['key-registry'] = inj.inject(keys)
        mouse = MouseRegistry()
        inj['mouse-registry'] = inj.inject(mouse)

        gman = inj.inject(GroupManager(map(inj.inject, cfg.groups())))
        cmd['groups'] = gman
        inj['group-manager'] = gman

        rules = inj['classifier'] = inj.inject(Classifier())
        for cls, cond, act in cfg.rules():
            rules.add_rule(cond, act, klass=cls)

        inj['event-dispatcher'] = inj.inject(EventDispatcher())
        inj['ewmh'] = Ewmh()
        inj.inject(inj['ewmh'])

        inj.inject(self)

        self.xcore.init_keymap()
        self.register_hotkeys(keys)
        mouse.init_buttons()
        mouse.register_buttons(self.root_window)
        self.setup_events()

        for screen_no, bar in cfg.bars():
            inj.inject(bar)
            if screen_no < len(screenman.screens):
                scr = screenman.screens[screen_no]
                if bar.position == 'bottom':
                    scr.add_bottom_bar(bar)
                else:
                    scr.add_top_bar(bar)
                bar.create_window()
                scr.updated.listen(bar.redraw.emit)

        self.catch_windows()
        signal.signal(signal.SIGCHLD, child_handler)
        signal.signal(signal.SIGQUIT, quit_handler)
        self.loop()

    def catch_windows(self):
        cnotify = self.xcore.proto.events['CreateNotify'].type
        mnotify = self.xcore.proto.events['MapRequest'].type
        for w in self.xcore.raw.QueryTree(window=self.root_window)['children']:
            if w == self.root_window or w in self.dispatcher.all_windows:
                continue
            attr = self.xcore.raw.GetWindowAttributes(window=w)
            if attr['class'] == self.xcore.WindowClass.InputOnly:
                continue
            geom = self.xcore.raw.GetGeometry(drawable=w)
            self.dispatcher.handle_CreateNotifyEvent(cnotify(0,
                window=w,
                parent=self.root_window.wid,
                x=geom['x'],
                y=geom['y'],
                width=geom['width'],
                height=geom['height'],
                border_width=geom['border_width'],
                override_redirect=attr['override_redirect'],
                ))
            win = self.dispatcher.windows[w]
            if(attr['map_state'] != self.xcore.MapState.Unmapped
                and not attr['override_redirect']):
                self.dispatcher.handle_MapRequestEvent(mnotify(0,
                    parent=self.root_window.wid,
                    window=w,
                    ))

    def setup_events(self):
        EM = self.xcore.EventMask
        self.xcore.raw.ChangeWindowAttributes(
            window=self.root_window,
            params={
                self.xcore.CW.EventMask: EM.StructureNotify
                                       | EM.SubstructureNotify
                                       | EM.SubstructureRedirect
            })
        attr = self.xcore.raw.GetWindowAttributes(window=self.root_window)
        if not (attr['your_event_mask'] & EM.SubstructureRedirect):
            print("Probably another window manager is running", file=sys.stderr)
            return

    def loop(self):
        for i in self.xcore.get_events():
            try:
                self.dispatcher.dispatch(i)
            except Exception:
                log.exception("Error handling event %r", i)

    def cmd_restart(self):
        os.execv(sys.executable, [sys.executable] + sys.argv)
