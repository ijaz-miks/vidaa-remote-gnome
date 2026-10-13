#!/usr/bin/python3
"""VIDAA Remote, a native GTK 4 and libadwaita desktop application."""
from __future__ import annotations
import argparse
import json
import sys
import threading
from pathlib import Path
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Adw, Gio, GLib, Gdk
from backend import (APP_ID, ROOT, Settings, DirectController, PhoneController, POOL,
                     discover, manual_device, wake)

parser = argparse.ArgumentParser(description='VIDAA TV remote for GNOME')
parser.add_argument('--preview', action='store_true', help='Open a UI preview without connecting to a TV')
parser.add_argument('--screenshot', help='Save a local UI verification screenshot')
parser.add_argument('--quit-after', type=int, default=0, help='Close after the specified seconds')
args = parser.parse_args()


class PreviewSettings:
    """Keep demo screenshots independent of real settings and credentials."""
    def __init__(self):
        self.data = {'uuid': '02:00:00:00:00:01', 'mode': 'direct', 'window': [720, 940],
                     'tv': {'name': 'Living Room TV', 'host': '192.0.2.10',
                            'model': 'VIDAA TV', 'firmware': '', 'mac': ''}}

    def save(self):
        pass

    def set_tv(self, tv):
        self.data['tv'] = dict(tv)
        return dict(tv)

CSS = '''
.remote-pad button { min-width: 54px; min-height: 54px; border-radius: 16px; font-weight: 600; }
.remote-pad button.select-key { min-width: 66px; min-height: 66px; border-radius: 50%; }
.remote-quick button { min-height: 36px; padding: 8px 16px; border-radius: 12px; }
.remote-volume button { min-width: 44px; min-height: 38px; border-radius: 12px; }
.status-pill { padding: 5px 12px; border-radius: 999px; background: alpha(@window_fg_color, 0.07); font-size: 0.9em; }
.status-pill.connected { color: @success_color; background: alpha(@success_color, 0.1); }
.volume-number { font-size: 22px; font-weight: 600; }
.section-heading { font-weight: 600; }
'''


def button(label=None, icon=None, callback=None, tooltip=None):
    b = Gtk.Button(label=label) if label is not None else Gtk.Button(icon_name=icon)
    if tooltip:
        b.set_tooltip_text(tooltip)
    if callback:
        b.connect('clicked', callback)
    return b


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='VIDAA Remote')
        self.settings = PreviewSettings() if args.preview else Settings()
        self.controller = None
        self.epoch = 0
        self.ready = False
        self.busy = False
        self.key_buttons = []
        self.sources = []
        self.apps = []
        self.pin_dialog = None
        self.volume_known = False
        self.updating_volume = False
        self.set_default_size(*self.settings.data.get('window', [720, 940]))
        self.set_size_request(400, 520)
        toolbar = Adw.ToolbarView()
        self.header_title = Adw.WindowTitle(title='VIDAA Remote', subtitle='Smart TV')
        header = Adw.HeaderBar(title_widget=self.header_title)
        menu = Gio.Menu()
        menu.append('About VIDAA Remote', 'app.about')
        menu.append('Quit', 'app.quit')
        header.pack_end(Gtk.MenuButton(icon_name='open-menu-symbolic', menu_model=menu))
        toolbar.add_top_bar(header)
        self.toast_overlay = Adw.ToastOverlay()
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22,
                       margin_top=18, margin_bottom=24, margin_start=24, margin_end=24)
        clamp = Adw.Clamp(maximum_size=660, tightening_threshold=520)
        clamp.set_child(body)
        scroll.set_child(clamp)
        self.scroller = scroll
        self.toast_overlay.set_child(scroll)
        toolbar.set_content(self.toast_overlay)
        self.set_content(toolbar)

        group = Adw.PreferencesGroup()
        self.device_row = Adw.ActionRow(title='Smart TV', subtitle='Select your VIDAA TV', use_markup=False)
        self.device_row.add_prefix(Gtk.Image(icon_name='video-display-symbolic', pixel_size=32))
        self.status_pill = Gtk.Label(label='Offline', valign=Gtk.Align.CENTER)
        self.status_pill.add_css_class('status-pill')
        self.device_row.add_suffix(self.status_pill)
        self.device_row.add_suffix(button(icon='edit-find-symbolic', callback=self.find_tvs, tooltip='Find or add a TV'))
        group.add(self.device_row)
        connection_row = Adw.ActionRow(title='Connection')
        self.mode = Gtk.DropDown.new_from_strings(['USB phone', 'Direct TV'])
        self.mode.set_selected(0 if self.settings.data.get('mode') == 'phone' else 1)
        self.mode.set_valign(Gtk.Align.CENTER)
        self.mode.set_tooltip_text('USB phone uses the paired VIDAA app. Direct TV uses a separate desktop pairing.')
        self.mode.connect('notify::selected', self.mode_changed)
        connection_row.add_suffix(self.mode)
        self.connect_button = button('Connect', callback=self.connect_tv)
        self.connect_button.set_valign(Gtk.Align.CENTER)
        connection_row.add_suffix(self.connect_button)
        group.add(connection_row)
        body.append(group)

        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.status_label = Gtk.Label(label='Connect to start controlling your TV.', wrap=True,
                                      xalign=0, hexpand=True)
        self.status_label.add_css_class('dim-label')
        status_line = Gtk.Box(spacing=10)
        self.spinner = Gtk.Spinner()
        status_line.append(self.spinner)
        status_line.append(self.status_label)
        status_box.append(status_line)
        pair_line = Gtk.Box(spacing=10)
        self.pair_button = button('Pair this desktop', callback=self.start_pairing)
        self.pair_button.add_css_class('suggested-action')
        self.pin_button = button('Enter TV code', callback=lambda *_: self.show_pin())
        pair_line.append(self.pair_button)
        pair_line.append(self.pin_button)
        self.pair_line = pair_line
        status_box.append(pair_line)
        body.append(status_box)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER)
        body.append(switcher)
        body.append(self.stack)
        self.stack.add_titled(self.remote_page(), 'remote', 'Remote')
        self.stack.add_titled(self.inputs_page(), 'inputs', 'Inputs')
        self.stack.add_titled(self.apps_page(), 'apps', 'TV apps')

        footer = Gtk.Box(spacing=8)
        self.wake_button = button('Wake TV', icon=None, callback=self.wake_tv)
        footer.append(self.wake_button)
        footer.append(button(icon='view-refresh-symbolic', callback=self.refresh_tv, tooltip='Refresh TV information'))
        self.detail_label = Gtk.Label(label='', wrap=True, xalign=1, hexpand=True)
        self.detail_label.add_css_class('dim-label')
        footer.append(self.detail_label)
        body.append(footer)
        self.sync_mode()
        self.update_device(self.settings.data.get('tv', {}))
        self.connect('close-request', self.closing)
        GLib.timeout_add(300, lambda: (self.scroller.get_vadjustment().set_value(0), False)[1])
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self.key_pressed)
        self.add_controller(keys)
        if args.preview:
            GLib.idle_add(self.preview)
        else:
            GLib.idle_add(self.connect_tv)
        if args.screenshot:
            GLib.timeout_add_seconds(3, self.capture)
        if args.quit_after:
            GLib.timeout_add_seconds(args.quit_after, lambda: (self.close(), False)[1])

    def remote_key(self, key, label=None, icon=None, tooltip=None):
        b = button(label, icon, lambda *_: self.send_key(key), tooltip)
        self.key_buttons.append((b, key))
        return b

    def remote_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        quick = Gtk.Box(spacing=12, homogeneous=True)
        quick.add_css_class('remote-quick')
        quick.append(self.remote_key('KEY_HOME', 'Home', tooltip='TV Home'))
        quick.append(self.remote_key('KEY_RETURNS', 'Back', tooltip='Return on TV'))
        quick.append(self.remote_key('KEY_MENU', 'Menu', tooltip='TV menu'))
        page.append(quick)
        pad = Gtk.Grid(row_spacing=8, column_spacing=8, halign=Gtk.Align.CENTER)
        pad.add_css_class('remote-pad')
        for key, icon, x, y, tip in [
                ('KEY_UP', 'pan-up-symbolic', 1, 0, 'Up'),
                ('KEY_LEFT', 'pan-start-symbolic', 0, 1, 'Left'),
                ('KEY_RIGHT', 'pan-end-symbolic', 2, 1, 'Right'),
                ('KEY_DOWN', 'pan-down-symbolic', 1, 2, 'Down')]:
            pad.attach(self.remote_key(key, icon=icon, tooltip=tip), x, y, 1, 1)
        ok = self.remote_key('KEY_OK', 'OK', tooltip='Select on TV')
        ok.add_css_class('select-key')
        ok.add_css_class('suggested-action')
        pad.attach(ok, 1, 1, 1, 1)
        page.append(pad)
        hint = Gtk.Label(label='Arrow keys navigate · Enter selects · Esc goes back', wrap=True)
        hint.add_css_class('dim-label')
        page.append(hint)
        group = Adw.PreferencesGroup(title='Sound')
        row = Adw.ActionRow(title='Volume')
        self.volume_label = Gtk.Label(label='--', valign=Gtk.Align.CENTER)
        self.volume_label.add_css_class('volume-number')
        controls = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
        controls.add_css_class('remote-volume')
        controls.append(self.remote_key('KEY_VOLUMEDOWN', icon='list-remove-symbolic', tooltip='Volume down'))
        controls.append(self.volume_label)
        controls.append(self.remote_key('KEY_VOLUMEUP', icon='list-add-symbolic', tooltip='Volume up'))
        controls.append(self.remote_key('KEY_MUTE', icon='audio-volume-muted-symbolic', tooltip='Mute or unmute'))
        row.add_suffix(controls)
        group.add(row)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.set_margin_start(12)
        self.scale.set_margin_end(12)
        self.scale.set_margin_top(8)
        self.scale.set_margin_bottom(8)
        self.scale.connect('value-changed', self.scale_changed)
        group.add(self.scale)
        page.append(group)
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        channels = Gtk.Box(spacing=10, homogeneous=True)
        channels.append(self.remote_key('KEY_CHANNELDOWN', 'Channel −'))
        channels.append(self.remote_key('KEY_CHANNELUP', 'Channel +'))
        bottom.append(channels)
        playback = Gtk.Box(spacing=10, homogeneous=True)
        for key, icon, name in [('KEY_PLAY', 'media-playback-start-symbolic', 'Play'),
                                ('KEY_PAUSE', 'media-playback-pause-symbolic', 'Pause'),
                                ('KEY_STOP', 'media-playback-stop-symbolic', 'Stop'),
                                ('KEY_POWER', 'system-shutdown-symbolic', 'TV power')]:
            playback.append(self.remote_key(key, icon=icon, tooltip=name))
        bottom.append(playback)
        page.append(bottom)
        return page

    def inputs_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        label = Gtk.Label(label='Choose the TV input you want to watch.', xalign=0, wrap=True)
        label.add_css_class('dim-label')
        page.append(label)
        self.source_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.source_list.add_css_class('boxed-list')
        self.source_list.set_placeholder(Gtk.Label(label='Connect to load TV inputs.', margin_top=30, margin_bottom=30))
        page.append(self.source_list)
        return page

    def apps_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.apps_hint = Gtk.Label(label='Pair directly with the TV to load its apps.', wrap=True, xalign=0)
        self.apps_hint.add_css_class('dim-label')
        page.append(self.apps_hint)
        self.app_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.app_list.add_css_class('boxed-list')
        self.app_list.set_placeholder(Gtk.Label(label='No TV apps loaded.', margin_top=30, margin_bottom=30))
        page.append(self.app_list)
        return page

    def toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast(title=str(text), timeout=5))

    def emit(self, epoch, kind, value):
        GLib.idle_add(self.event, epoch, kind, value)

    def event(self, epoch, kind, value):
        if epoch != self.epoch:
            return False
        if kind == 'status':
            state = value['state']
            self.ready = state == 'ready'
            self.busy = state == 'connecting'
            self.status_label.set_label(value['text'])
            self.status_pill.set_label({'ready': 'Connected', 'connecting': 'Connecting',
                                       'pairing': 'Pairing needed', 'offline': 'Offline',
                                       'error': 'Unavailable'}.get(state, state))
            if self.ready:
                self.status_pill.add_css_class('connected')
            else:
                self.status_pill.remove_css_class('connected')
            self.spinner.set_spinning(self.busy)
            self.spinner.set_visible(self.busy)
            self.connect_button.set_sensitive(not self.busy)
            self.connect_button.set_label('Reconnect' if self.controller else 'Connect')
            self.sync_controls()
        elif kind == 'volume':
            self.volume_known = value is not None
            self.volume_label.set_label(str(value) if value is not None else '--')
            if value is not None:
                self.updating_volume = True
                self.scale.set_value(value)
                self.updating_volume = False
            self.sync_controls()
        elif kind == 'muted':
            self.volume_label.set_tooltip_text('Muted' if value else 'Sound on')
        elif kind == 'device':
            self.update_device(value)
        elif kind == 'sources':
            self.sources = value
            self.populate_sources()
        elif kind == 'apps':
            self.apps = value
            self.populate_apps()
        elif kind == 'pin-needed':
            self.show_pin()
        elif kind == 'paired':
            if self.pin_dialog:
                self.pin_dialog.close()
            self.pin_dialog = None
            self.toast('Desktop paired with TV. Pairing saved in GNOME Keyring.' if value.get('saved') else
                       'Desktop paired for this session. GNOME Keyring could not save the pairing.')
        elif kind == 'pair-requested':
            self.status_label.set_label('Requesting a separate pairing for Ubuntu. Check the TV for a four-digit code.')
            GLib.timeout_add_seconds(15, self.pair_timeout, epoch)
        elif kind == 'pair-acknowledged':
            self.status_label.set_label('TV accepted Ubuntu’s pairing request. Enter the code if it appears on the TV.')
        elif kind == 'error':
            self.toast(value)
        return False

    def pair_timeout(self, epoch):
        if epoch == self.epoch and not self.ready:
            confirmed = isinstance(self.controller, DirectController) and self.controller.pairing_confirmed
            self.status_label.set_label('Ubuntu pairing is pending. Enter the code shown on the TV.' if confirmed else
                                       'The TV has not acknowledged Ubuntu’s pairing request. Reconnect and try Pair this desktop again.')
        return False

    def update_device(self, tv):
        self.header_title.set_subtitle(tv.get('name', 'Smart TV'))
        self.device_row.set_title(tv.get('name', 'Smart TV'))
        parts = [tv.get('model', ''), tv.get('host', '')]
        self.device_row.set_subtitle(' · '.join(x for x in parts if x) or 'Find your VIDAA TV')
        self.detail_label.set_label(tv.get('firmware', ''))
        self.wake_button.set_sensitive(bool(tv.get('mac')))

    def sync_mode(self):
        direct = self.mode.get_selected() == 1
        self.pair_line.set_visible(direct)
        self.scale.set_visible(direct)
        self.apps_hint.set_label('Connect directly to load and launch TV apps.' if direct else
                                 'TV apps require a direct pairing. Home opens the TV launcher in USB phone mode.')
        self.sync_controls()

    def sync_controls(self):
        mode = 'direct' if self.mode.get_selected() == 1 else 'phone'
        for b, key in self.key_buttons:
            supported = mode == 'direct' or key in PhoneController.supported_keys
            b.set_sensitive(self.ready and supported)
            if not supported:
                b.set_tooltip_text('Available with a direct TV pairing')
        self.scale.set_sensitive(self.ready and self.volume_known)
        connected = bool(self.controller and self.controller.mode == 'direct' and
                         self.controller.client and self.controller.client.is_connected())
        self.pair_button.set_sensitive(connected and not self.ready)
        self.pin_button.set_sensitive(connected and not self.ready)
        self.source_list.set_sensitive(self.ready)
        self.app_list.set_sensitive(self.ready and mode == 'direct')

    def mode_changed(self, *_):
        self.settings.data['mode'] = 'phone' if self.mode.get_selected() == 0 else 'direct'
        self.settings.save()
        self.sync_mode()
        self.connect_tv()

    def run(self, action, *, serial=False):
        epoch = self.epoch
        controller = self.controller
        def work():
            try:
                if epoch != self.epoch:
                    return
                action()
            except Exception as exc:
                self.emit(epoch, 'error', str(exc))
                if controller and not controller.ready:
                    self.emit(epoch, 'status', {'state': 'error', 'text': str(exc)})
        executor = controller.actions if serial and isinstance(controller, PhoneController) else POOL
        executor.submit(work)

    def connect_tv(self, *_):
        if args.preview:
            return False
        tv = self.settings.data.get('tv', {})
        if not tv.get('host'):
            self.find_tvs()
            return False
        self.epoch += 1
        if self.controller:
            old = self.controller
            POOL.submit(old.close)
        epoch = self.epoch
        cls = PhoneController if self.mode.get_selected() == 0 else DirectController
        self.controller = cls(self.settings, lambda kind, value: self.emit(epoch, kind, value))
        self.ready = False
        self.event(epoch, 'status', {'state': 'connecting', 'text': 'Connecting…'})
        self.run(self.controller.connect)
        return False

    def send_key(self, key):
        if args.preview:
            return
        if self.ready and self.controller:
            controller = self.controller
            self.run(lambda: controller.send_key(key), serial=True)

    def scale_changed(self, *_):
        if self.updating_volume or not self.ready or not self.volume_known or args.preview:
            return
        if hasattr(self, 'volume_timeout') and self.volume_timeout:
            GLib.source_remove(self.volume_timeout)
        self.volume_timeout = GLib.timeout_add(250, self.apply_volume)

    def apply_volume(self):
        self.volume_timeout = None
        if self.ready and isinstance(self.controller, DirectController):
            controller = self.controller
            value = int(self.scale.get_value())
            self.run(lambda: controller.set_volume(value))
        return False

    def refresh_tv(self, *_):
        if self.controller and self.ready and not args.preview:
            self.run(self.controller.refresh, serial=True)

    def wake_tv(self, *_):
        if not args.preview:
            self.run(lambda: wake(self.settings.data['tv']))
            self.toast('Wake request sent. Reconnect when the TV is ready.')

    def start_pairing(self, *_):
        if isinstance(self.controller, DirectController):
            self.run(self.controller.pair)

    def show_pin(self):
        if self.pin_dialog:
            self.pin_dialog.present(self)
            return
        if not isinstance(self.controller, DirectController):
            return
        dialog = Adw.AlertDialog(heading='Pair with your TV',
                                 body='Enter the four-digit code displayed on your TV.')
        entry = Gtk.Entry(placeholder_text='Four-digit TV code', max_length=4,
                          input_purpose=Gtk.InputPurpose.DIGITS, activates_default=True,
                          halign=Gtk.Align.CENTER)
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('pair', 'Pair')
        dialog.set_response_appearance('pair', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('pair')
        dialog.set_close_response('cancel')
        def response(_dialog, result):
            self.pin_dialog = None
            if result == 'pair':
                pin = entry.get_text().strip()
                controller = self.controller
                self.run(lambda: controller.submit_pin(pin))
        dialog.connect('response', response)
        self.pin_dialog = dialog
        dialog.present(self)

    def populate_sources(self):
        self.source_list.remove_all()
        for source in self.sources:
            if not isinstance(source, dict) or 'sourceid' not in source:
                continue
            name = source.get('displayname') or source.get('sourcename') or str(source['sourceid'])
            row = Adw.ActionRow(title=name, use_markup=False)
            row.add_prefix(Gtk.Image(icon_name='video-display-symbolic'))
            row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
            row.set_activatable(True)
            row.connect('activated', lambda _, s=source: self.choose_source(s))
            self.source_list.append(row)

    def choose_source(self, source):
        if self.ready and self.controller and not args.preview:
            controller = self.controller
            self.run(lambda: controller.set_source(source), serial=True)

    def populate_apps(self):
        self.app_list.remove_all()
        count = 0
        for app in self.apps:
            if not isinstance(app, dict):
                continue
            name = app.get('name') or app.get('appName') or app.get('title')
            if not name:
                continue
            row = Adw.ActionRow(title=str(name), use_markup=False)
            row.add_prefix(Gtk.Image(icon_name='application-x-executable-symbolic'))
            row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
            row.set_activatable(True)
            row.connect('activated', lambda _, a=app: self.launch_tv_app(a))
            self.app_list.append(row)
            count += 1
        self.apps_hint.set_label('Choose an app to open it on the TV.' if count else
                                 'This TV has not returned an app list. Home opens its launcher.')

    def launch_tv_app(self, app):
        if self.ready and isinstance(self.controller, DirectController) and not args.preview:
            controller = self.controller
            self.run(lambda: controller.launch_app(app))

    def find_tvs(self, *_):
        dialog = Adw.Dialog(title='Find a TV', content_width=440, content_height=440)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                      margin_start=20, margin_end=20, margin_top=18, margin_bottom=20)
        label = Gtk.Label(label='Searching your network…', wrap=True, xalign=0)
        box.append(label)
        results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        results.add_css_class('boxed-list')
        box.append(results)
        entry = Gtk.Entry(placeholder_text='TV IP address, for example 192.168.1.100')
        manual = button('Add by address')
        box.append(entry)
        box.append(manual)
        toolbar.set_content(box)
        dialog.set_child(toolbar)
        def choose(tv):
            self.settings.set_tv(tv)
            self.update_device(self.settings.data['tv'])
            dialog.close()
            self.connect_tv()
        def show(found):
            label.set_label('Select your TV.' if found else 'No TV found. Turn it on, or add its address.')
            for tv in found:
                row = Adw.ActionRow(title=tv['name'], subtitle=tv['host'], activatable=True, use_markup=False)
                row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
                row.connect('activated', lambda _, t=tv: choose(t))
                results.append(row)
            return False
        def scan():
            try:
                found = discover()
                GLib.idle_add(show, found)
            except Exception:
                GLib.idle_add(label.set_label, 'Discovery failed. You can add the TV by address.')
        def add(_):
            host = entry.get_text().strip()
            manual.set_sensitive(False)
            def lookup():
                try:
                    tv = manual_device(host)
                    GLib.idle_add(choose, tv)
                except Exception as exc:
                    GLib.idle_add(label.set_label, str(exc))
                    GLib.idle_add(manual.set_sensitive, True)
            POOL.submit(lookup)
        manual.connect('clicked', add)
        dialog.present(self)
        POOL.submit(scan)

    def key_pressed(self, controller, keyval, keycode, state):
        if not self.ready or self.pin_dialog or self.get_visible_dialog() or self.stack.get_visible_child_name() != 'remote' or state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        focus = self.get_focus()
        if focus and (isinstance(focus, Gtk.Editable) or focus.get_ancestor(Gtk.DropDown) or
                      focus.get_ancestor(Gtk.Scale) or focus is self.connect_button):
            return False
        mapping = {Gdk.KEY_Up: 'KEY_UP', Gdk.KEY_Down: 'KEY_DOWN', Gdk.KEY_Left: 'KEY_LEFT',
                   Gdk.KEY_Right: 'KEY_RIGHT', Gdk.KEY_Return: 'KEY_OK', Gdk.KEY_KP_Enter: 'KEY_OK',
                   Gdk.KEY_Escape: 'KEY_RETURNS', Gdk.KEY_Home: 'KEY_HOME', Gdk.KEY_Menu: 'KEY_MENU'}
        if keyval in mapping:
            self.send_key(mapping[keyval])
            return True
        return False

    def preview(self):
        self.ready = True
        self.event(self.epoch, 'status', {'state': 'ready', 'text': 'Demo: connected directly to TV'})
        self.event(self.epoch, 'volume', 15)
        self.event(self.epoch, 'sources', [{'sourceid': x, 'sourcename': x} for x in ['TV', 'HDMI1', 'HDMI2', 'HDMI3', 'HDMI4', 'AV']])
        return False

    def capture(self):
        try:
            gi.require_version('Graphene', '1.0')
            from gi.repository import Graphene
            paintable = Gtk.WidgetPaintable.new(self)
            snapshot = Gtk.Snapshot()
            width, height = self.get_width(), self.get_height()
            paintable.snapshot(snapshot, width, height)
            node = snapshot.to_node()
            renderer = self.get_native().get_renderer()
            rect = Graphene.Rect()
            rect.init(0, 0, width, height)
            texture = renderer.render_texture(node, rect)
            if Path(args.screenshot).exists():
                raise FileExistsError('Screenshot destination already exists; it was preserved.')
            texture.save_to_png(args.screenshot)
            print('Screenshot saved:', args.screenshot, flush=True)
        except Exception as exc:
            print('Screenshot failed:', str(exc), flush=True)
        return False

    def closing(self, *_):
        self.epoch += 1
        if self.controller:
            POOL.submit(self.controller.close)
        if not args.preview:
            self.settings.data['window'] = [max(400, self.get_width()), max(520, self.get_height())]
            self.settings.save()
        return False


class Application(Adw.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.NON_UNIQUE if args.preview else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=APP_ID, flags=flags)
        self.connect('activate', self.activate_window)
        for name, callback in [('quit', lambda *_: self.quit()), ('about', self.about)]:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            self.add_action(action)
        self.set_accels_for_action('app.quit', ['<primary>q'])

    def activate_window(self, *_):
        window = self.get_active_window()
        if not window:
            provider = Gtk.CssProvider()
            provider.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            window = Window(self)
        window.present()

    def about(self, *_):
        dialog = Adw.AboutDialog(application_name='VIDAA Remote',
                  application_icon=APP_ID, version='0.1.0',
                  comments='A local TV remote for GNOME. Direct TV pairing and USB phone control.',
                  license_type=Gtk.License.MIT_X11)
        dialog.present(self.get_active_window())


def main():
    application = Application()
    return application.run([sys.argv[0]])


if __name__ == '__main__':
    raise SystemExit(main())
