import os
import sys
import asyncio
import subprocess
import platform
import ctypes
import webbrowser
from pathlib import Path
import psutil
import crossfiledialog
import nicegui as ng
from nicegui import app, ui
import configmanager
import util
import requests
import atexit
import bootstrapper
import time


app.middleware_stack = None
app.native.window_args['resizable'] = False

task = None

can_edit_settings = False


def validate_config():
    util.check_for_updates()
    if not configmanager.ensure_config_file():
        response = util.win_message_box(
            'Your configuration file is either corrupted or inaccessible to the launcher. Do you want to correct this error?',
            'Configuration file error',
            util.MB_YESNO
        )
        if response != 7:
            try:
                os.remove(configmanager.CONFIG_FILE)
                if not configmanager.ensure_config_file():
                    util.win_message_box(
                        'We were unable to fix the error. Please contact support and provide the following information:\n\nRecreated config is invalid.',
                        'Unable to fix',
                        util.MB_OK
                    )
                    sys.exit()
            except Exception as err:
                util.win_message_box(
                    f'We were unable to fix the error. Please contact support and provide the following information:\n\n{err}',
                    'Unable to fix',
                    util.MB_OK
                )
                sys.exit()
        else:
            sys.exit()
    selected_server = configmanager.get_config_value("selected_server").lower()

    if selected_server == "ppy.sh":
        util.win_message_box(
            "We have detected that you modified the launcher config manually in order to connect to the official Bancho servers. "
            "We disallow this to keep your account safe. Mods used here inject directly into the osu! process or even modify the game files. "
            "Such actions are likely to trigger an autoban on Bancho.",
            'WARNING',
            util.MB_OK
        )
        sys.exit()
    if selected_server not in {"m1pposu.dev", "4ayosu.ovh"}:
        util.win_message_box(
            "We have detected that you modified the launcher config manually in order to connect to unsupported servers. "
            "Using some mods included here might result in a ban when used outside M1PP & 4ayo. Proceed with caution.",
            'WARNING',
            util.MB_OK
        )
        return

app.on_startup(validate_config)


def cleanup():
    for p in psutil.process_iter(['pid','name']):
        name = (p.info['name'] or '').lower()
        if name in ('osu!.exe', 'tosu.exe', 'osu!.patcher.exe'):
            try:
                print(f"Killing process PID={p.pid} Name={p.info['name']}")
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

def on_tab_change(event):
    global can_edit_settings
    global tab_block_html
    if event.value == 'a':
        launch_info_card.visible = configmanager.get_config_value("launch_info")
    if not can_edit_settings:
        tab_block_html = ui.html('''
            <style>
            .tb-sw-tg {
                pointer-events: none;
            }
            </style>
        ''')

def launch_handler(tabs, ssel, lbtn, progress_label):
    global task
    task = asyncio.create_task(launch_osu(tabs, ssel, lbtn, progress_label))

# Unfortunately there isn't a way to just hide the window, you have to use the Windows API :c
def set_window_visibility(title: str, visible: bool) -> bool:
    SW_HIDE = 0
    SW_SHOW = 5

    FindWindow = ctypes.windll.user32.FindWindowW
    ShowWindow = ctypes.windll.user32.ShowWindow

    hwnd = FindWindow(None, title)
    if hwnd:
        ShowWindow(hwnd, SW_SHOW if visible else SW_HIDE)
        return True
    else:
        return False

def toggle_mod(name, value):
    global debug_mods
    configmanager.update_mod(name, value)
    debug_mods.set_text('Mods: {}'.format(configmanager.get_config_value("mods_enabled")))
    debug_mods.update()


async def launch_osu(tabs, ssel, lbtn, progress_label):
    try:
        lbtn.disable()
        set_tab_change_state(tabs, ssel, False)

        osu_path = configmanager.get_config_value("osu_path")
        osu_exe = os.path.join(osu_path, "osu!.exe")
        if not os.path.isfile(osu_exe):
            try:
                selected_folder = bootstrapper.default_game_path
                os.makedirs(selected_folder, exist_ok=True)
                if not selected_folder:
                    util.win_message_box("Invalid folder", 'Error', util.MB_OK | util.MB_ICONERROR)
                    return

                if not os.listdir(selected_folder):
                    configmanager.set_config_value("osu_path", selected_folder)
                    osu_path = selected_folder
                else:
                    util.win_message_box(
                        "The {}\\osu!m1pp folder has to be empty".format(os.getenv("LOCALAPPDATA")), 'Error', util.MB_OK | util.MB_ICONERROR
                    )
                    return
            except Exception as err:
                util.win_message_box(
                    "This location is inaccessible (no write permission)\n\n" + err,
                    'Error', util.MB_OK | util.MB_ICONERROR
                )
                return

        # prepare for launching
        pathdir = configmanager.get_config_value("osu_path")
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        loadosu = True
        while loadosu:
            progress_label.set_text('Bootstraping osu!...')
            pathdir = configmanager.get_config_value("osu_path")
            res = await bootstrapper.async_bootstrap_osu(pathdir)
            if res == 1:
                # user cancelled or bootstrapper signaled abort
                return

            progress_label.set_text('Launching osu!...')
            proc = subprocess.Popen(
                [os.path.join(pathdir, "osu!.exe"),
                 "-devserver", configmanager.get_config_value("selected_server")],
                cwd=pathdir,
                startupinfo=startupinfo
            )
            if configmanager.get_config_value("launcher_hide_startup"):
                set_window_visibility("M1PP Launcher", False)
            # reset our flags per‐launch
            tosu_injected = False
            rp_injected = False
            presentosu = False

            # wait for osu! to appear, or for the process to exit
            while proc.poll() is None:
                try:
                    for i in range(2):
                        if util.is_osu_window_present():
                            presentosu = True
                            await asyncio.sleep(0.1)
                        else:
                            presentosu = False
                            break
                    process = None
                    for p in psutil.process_iter(['pid','name']):
                        if p.info['name'] and p.info['name'].lower() == 'osu!.exe':
                            process = p
                            break

                    # if we didn’t see the process yet, wait up to 10 s then give up this launch
                    if not process:
                        timeout_start = time.time()
                        while (time.time() - timeout_start) < 10 and not process:
                            await asyncio.sleep(0.5)
                            for p in psutil.process_iter(['pid','name']):
                                if p.info['name'] and p.info['name'].lower() == 'osu!.exe':
                                    process = p
                                    break
                        if not process:
                            print("osu!.exe process not found. Restarting launch.")
                            break

                    # once we have a process, check its cmdline for the right server
                    if process:
                        cmd = process.cmdline()
                        if configmanager.get_config_value("selected_server") not in cmd:
                            util.win_message_box(
                                'osu! has updated. Please launch the game again.',
                                'osu! update',
                                util.MB_OK | util.MB_ICONINFORMATION
                            )
                            loadosu = False
                            break

                    # inject tosu.exe if enabled
                    mods = configmanager.get_config_value("mods_enabled")
                    if "tosu" in mods and not tosu_injected:
                        subprocess.Popen(
                            [os.path.join(pathdir, "tosu.exe")],
                            cwd=pathdir,
                            startupinfo=startupinfo
                        )
                        tosu_injected = True
                    # inject RelaxPatcher if enabled
                    if "RelaxPatcher" in mods and not rp_injected:
                        patcher_proc = subprocess.Popen(
                            [os.path.join(pathdir, "relaxpatcher", "osu!.patcher.exe")],
                            cwd=pathdir,
                            startupinfo=startupinfo,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True
                        )
                        
                        try:
                            # read its output once to verify it runs without exception
                            out = patcher_proc.stdout.readline()
                            if out and "System.Exception" not in out and out != "\n":
                                print("RelaxPatcher output: ", out)
                                rp_injected = True
                        except Exception as e:
                            print(f"Patch error: {e}")
                        finally:
                            patcher_proc.kill()

                except Exception:
                    pass

                await asyncio.sleep(0.5)

            await asyncio.sleep(0.01)

            for p in psutil.process_iter(['pid','name']):
                name = (p.info['name'] or '').lower()
                if name in ('osu!.exe', 'tosu.exe', 'osu!.patcher.exe'):
                    try:
                        print(f"Killing process PID={p.pid} Name={p.info['name']}")
                        p.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        pass

            # if we saw the real osu! window, exit retry loop; otherwise, keep looping
            if presentosu:
                loadosu = False

    except Exception as err:
        util.win_message_box(
            f'We were unable to launch the game. Please contact support and provide the following information:\n\n{err}',
            'Unable to launch',
            util.MB_OK
        )
        loadosu = False

    finally:
        lbtn.enable()
        progress_label.set_text('')
        set_tab_change_state(tabs, ssel, True)
        if configmanager.get_config_value("launcher_hide_startup"):
            set_window_visibility("M1PP Launcher", True)

def set_tab_change_state(tabs, ssel, state):
    if state:
        tabs.style("pointer-events: auto;")
        ssel.style("pointer-events: auto;")
    else:
        tabs.style("pointer-events: none;")
        ssel.style("pointer-events: none;")

def on_toggle_change(e):
    global debug_server
    configmanager.set_config_value("selected_server", e.args[1]["label"])
    debug_server.set_text('Server: {}'.format(configmanager.get_config_value("selected_server")))

@ui.page('/')
def main():
    ui.add_head_html('''
    <style>
        html, body {
            margin: 0;
            height: 100%;
            overflow: hidden;
        }

        .nicegui-content {
            height: 100% !important;
            overflow: hidden !important;
            display: flex;
            flex-direction: column;
        }

        .q-tab-panels, .q-tab-panel {
            flex-grow: 1;
            height: 100%;
            overflow: hidden;
        }
                     
        @keyframes fadep {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        .fade-animation {
            animation-timing-function: ease-in-out;
            opacity: 0;
        }
        .animate {
            animation: fadep 1s forwards;        
        }            
        /* --- TABS --- */
        .dark .q-tabs {
            background-color: transparent !important;
            border-bottom: 1px solid #333 !important;
        }

        .dark .q-tab {
            color: #aaa !important;
            background-color: transparent !important;
            transition: color 0.2s ease;
        }

        .dark .q-tab:hover {
            color: #ffc4ff !important;
        }

        .dark .q-tab--active {
            color: #ffc4ff !important;
            font-weight: 600;
            border-bottom: 2px solid #ffc4ff !important;
        }

        .dark .q-tab-panels {
            background-color: transparent !important;
        }
    </style>
    ''')
    ui.add_head_html('''
        <script>
        window.onload = () => {
            setTimeout(() => {
                const elw = document.querySelector('.fade-animation');
                const elements = document.querySelectorAll('.fade-animation');
                elements.forEach(el => {
                    el.classList.add('animate');
                    
                });
                elw.addEventListener('animationend', () => {
                    elements.forEach(el => {
                        el.style.opacity = '1';
                        el.classList.remove('fade-animation');
                        el.classList.remove('animate');
                    });
                });
            }, 100);
        };
        </script>
    ''')
    ui.run_javascript("""
    document.addEventListener('keydown', function(event) {
        if ((event.ctrlKey || event.metaKey) && event.key === 's') {
        event.preventDefault();
        }
    });
    """)
    if configmanager.get_config_value("dark_mode"):
        ui.dark_mode().enable()
    ui.colors(primary='#e4ade4', brand='#ffc4ff')
    ui.page_title('M1PP Launcher')
    with ui.tabs().classes('w-full fade-animation') as tabs:
        ui.tab('a', label='Home').classes('px-12')
        ui.tab('b', label='News').classes('px-12')
        ui.tab('c', label='Settings').classes('px-12')
        ui.tab('d', label='Runtime').classes('px-12')
        ui.tab('e', label='About').classes('px-12')
    with ui.tab_panels(tabs, value='a', on_change=on_tab_change).classes('w-full'):
        with ui.tab_panel('a').classes("overflow-hidden"):
            global progress_label
            global debug_mods
            global debug_server
            with ui.column().classes("h-96 w-full align-middle flex justify-center items-center overflow-hidden fade-animation"):
                server = configmanager.get_config_value("selected_server")
                if server == "4ayosu.ovh":
                    serverid = 2
                else:
                    serverid = 1
                toggle = ui.toggle({1: 'm1pposu.dev', 2: '4ayosu.ovh'}, value=serverid)
                toggle.on('update:modelValue', on_toggle_change)
                lbtn = ui.button('Launch', on_click=lambda: launch_handler(tabs, toggle, lbtn, progress_label)).classes('px-12 mt-7')
                progress_label = ui.label('')

                global launch_info_card
                with ui.card().classes('w-full max-w-lg p-6 items-center gap-0 overflow-hidden') as launch_info_card:
                    ui.label('Launch info').classes('text-l font-bold gap-2')
                    ui.label('Installation: Separate')
                    ui.label('Platform: {}'.format(platform.system()))
                    debug_mods = ui.label('Mods: {}'.format(configmanager.get_config_value("mods_enabled")))
                    debug_server = ui.label('Server: {}'.format(configmanager.get_config_value("selected_server")))
        with ui.tab_panel('b'):
            with ui.column().classes("h-full w-full align-middle flex justify-center items-center"):
                ui.label('Latest News').classes('text-xl font-bold')
                with ui.scroll_area().classes('space-y-0 py-0 w-full h-80 overflow-hidden'):
                    with ui.column().classes('flex-grow w-full items-center justify-center'):
                        for news in requests.get("https://launcher.m1pposu.dev/news.json").json():                
                            with ui.card().classes('w-full max-w-lg p-4'):
                                ui.label(news["title"]).classes('text-xl font-bold gap-2')
                                ui.label(news["content"])
                                ui.button(news["button"], on_click=lambda: webbrowser.open(news["button_link"]))
        with ui.tab_panel('c'):
            value1 = configmanager.get_config_value("launcher_hide_startup")
            value2 = configmanager.get_config_value("launch_info")
            value3 = configmanager.get_config_value("animations")
            value4 = configmanager.get_config_value("dark_mode")

            ui.label('General').classes('text-xl font-bold gap-2')
            ui.switch('Hide launcher on game startup', value=configmanager.get_config_value("launcher_hide_startup"), on_change=lambda e: configmanager.set_config_value("launcher_hide_startup", e.sender.value))
            ui.switch('Show launch info', value=configmanager.get_config_value("launch_info"), on_change=lambda e: (configmanager.set_config_value("launch_info", e.sender.value)))
            # ui.switch('Play animations', value=configmanager.get_config_value("animations"), on_change=lambda e: configmanager.set_config_value("animations", e.sender.value))
            ui.switch('Dark mode', value=value4, on_change=lambda e: (ui.run_javascript('location.reload();'), configmanager.set_config_value("dark_mode", e.value), ui.run_javascript('location.reload();')))



        with ui.tab_panel('d').classes("overflow-hidden"):
            mods_enabled = configmanager.get_config_value("mods_enabled")

            ui.label('Mods').classes('text-xl font-bold gap-2')
            ui.switch('RelaxPatcher (rushiiMachine)', value="RelaxPatcher" in mods_enabled, on_change=lambda e: toggle_mod("RelaxPatcher", e.sender.value))
            ui.switch('tosu (tosu contributors)', value="tosu" in mods_enabled, on_change=lambda e: toggle_mod("tosu", e.sender.value))
            ui.switch('AssetPatcher (M1PP)', value="AssetPatcher" in mods_enabled, on_change=lambda e: toggle_mod("AssetPatcher", e.sender.value))
            ui.label('Game enviroment').classes('text-xl font-bold gap-2')
            ui.switch('Separate osu! install (Required for now)', value=True).props('disable')

        with ui.tab_panel('e').classes("overflow-hidden"):
            with ui.scroll_area().classes('space-y-0 py-0 w-full h-96 overflow-hidden'):
                with ui.column().classes('h-full w-full items-center justify-center'):
                    
                    with ui.card().classes('w-96 shadow-lg'):
                        ui.label('M1PP Launcher Source Code').classes('text-xl font-semibold')
                        ui.label('The source code of this launcher').classes('text-sm text-gray-500')
                        ui.button('GitHub', icon='code', on_click=lambda: webbrowser.open("https://github.com/m1pp/launcher")) \
                            .classes('bg-blue-500 hover:bg-blue-600 text-white rounded-full px-4 py-2')
                    
                    with ui.card().classes('w-96 shadow-lg'):
                        ui.label('M1PP/4ayo Discord').classes('text-xl font-semibold')
                        ui.label('Join our Discord server to share your plays, chat with others, report bugs, and suggest improvements!').classes('text-sm text-gray-500')
                        ui.button('Discord', icon='discord', on_click=lambda: webbrowser.open("https://dsc.gg/m1ppand4ayo")) \
                            .classes('bg-purple-500 hover:bg-purple-600 text-white rounded-full px-4 py-2')
                    with ui.card().classes('w-96 shadow-lg'):
                        ui.label('tosu GitHub').classes('text-xl font-semibold')
                        ui.label('Memory reader and PP counters provider for osu! and osu! Lazer') \
                            .classes('text-sm text-gray-500')
                        ui.button('GitHub', icon='extension', on_click=lambda: webbrowser.open("https://github.com/tosuapp/tosu")) \
                            .classes('text-white rounded-full px-4 py-2')
                    with ui.card().classes('w-96 shadow-lg'):
                        ui.label('osu-patcher Github (fork)').classes('text-xl font-semibold')
                        ui.label('osu!stable patcher for adding extra features') \
                            .classes('text-sm text-gray-500')
                        ui.button('GitHub', icon='extension', on_click=lambda: webbrowser.open("https://github.com/rushiiMachine/osu-patcher")) \
                            .classes('text-white rounded-full px-4 py-2')


    
if __name__ in {"__main__", "__mp_main__"}:
    atexit.register(cleanup)
    ui.run(native=True, window_size=(970, 530), fullscreen=False, reload=False, title='M1PP Launcher', favicon=util.resource_path("icon.ico"), reconnect_timeout=99999, port=64821)

