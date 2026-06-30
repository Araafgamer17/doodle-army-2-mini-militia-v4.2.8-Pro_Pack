import os
import sys
import requests
import json
import zipfile
import time

# --- ANSI COLOR PALETTE ---
C_BLUE    = "\033[1;34m"
C_CYAN    = "\033[1;36m"
C_GREEN   = "\033[1;32m"
C_YELLOW  = "\033[1;33m"
C_RED     = "\033[1;31m"
C_MAGENTA = "\033[1;35m"
C_WHITE   = "\033[1;37m"
C_GRAY    = "\033[90m"
C_RESET   = "\033[0m"
C_BOLD    = "\033[1m"

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

shell_state = {
    "os": "linux"
}

def log_info(msg):    print(f"{C_CYAN}[ℹ]{C_RESET} {msg}")
def log_success(msg): print(f"{C_GREEN}[✔]{C_RESET} {msg}")
def log_warn(msg):    print(f"{C_YELLOW}[⚠]{C_RESET} {msg}")
def log_error(msg):   print(f"{C_RED}[✘]{C_RESET} {msg}")

def is_lib_allowed(lib, target_os):
    rules = lib.get('rules')
    if not rules:
        return True
    allowed = False
    for rule in rules:
        rule_os = rule.get('os', {})
        if 'name' in rule_os:
            if rule_os['name'] == target_os:
                allowed = (rule.get('action') == 'allow')
        else:
            allowed = (rule.get('action') == 'allow')
    return allowed

def parse_library(lib, target_os):
    if not is_lib_allowed(lib, target_os):
        return []
    results = []
    downloads = lib.get('downloads', {})
    
    if 'artifact' in downloads:
        art = downloads['artifact']
        if art.get('url') and art.get('path'):
            results.append((f"libraries/{art['path']}", art['url']))
            
    if 'natives' in lib and target_os in lib['natives']:
        classifier_key = lib['natives'][target_os].replace('${arch}', '64')
        if 'classifiers' in downloads and classifier_key in downloads['classifiers']:
            cls_data = downloads['classifiers'][classifier_key]
            if cls_data.get('url') and cls_data.get('path'):
                results.append((f"libraries/{cls_data['path']}", cls_data['url']))
                
    if not results and 'name' in lib:
        parts = lib['name'].split(':')
        if len(parts) >= 3:
            group, artifact, version = parts[0], parts[1], parts[2]
            group_path = group.replace('.', '/')
            classifier = parts[3] if len(parts) > 3 else None
            filename = f"{artifact}-{version}.jar" if not classifier else f"{artifact}-{version}-{classifier}.jar"
            path = f"{group_path}/{artifact}/{version}/{filename}"
            results.append((f"libraries/{path}", f"https://libraries.minecraft.net/{path}"))
    return results

def draw_progress_bar(current, total, start_time, prefix=""):
    # Shortened bar length (12 slots) to ensure it NEVER wraps on narrow phone screens
    bar_length = 12
    fraction = current / total if total > 0 else 0
    filled_length = int(round(bar_length * fraction))
    
    bar = f"{C_GREEN}{'=' * filled_length}{C_GRAY}{' ' * (bar_length - filled_length)}{C_RESET}"
    
    # Live ETA engine logic
    elapsed = time.time() - start_time
    if current > 0 and fraction < 1.0:
        eta_secs = int((elapsed / current) * (total - current))
        if eta_secs > 3600:
            eta_str = f"{eta_secs//3600}h"
        elif eta_secs > 60:
            eta_str = f"{eta_secs//60}m{eta_secs%60}s"
        else:
            eta_str = f"{eta_secs}s"
    elif fraction >= 1.0:
        eta_str = "0s"
    else:
        eta_str = "--"
        
    # Strictly bound maximum string width to protect horizontal screen space
    short_prefix = prefix[:10]
    sys.stdout.write(f"\r\033[K{C_WHITE}{short_prefix:<10}{C_RESET} [{bar}] {C_CYAN}{current}/{total}{C_RESET} {C_YELLOW}ETA:{eta_str}{C_RESET}")
    sys.stdout.flush()

def download_engine(version_id, url, mode):
    target_os = shell_state["os"]
    os.makedirs("downloads", exist_ok=True)
    zip_path = f"downloads/mc_{version_id}_{target_os}_{mode}.zip"
    
    print(f"\n{C_MAGENTA}{'─'*50}{C_RESET}")
    log_info(f"Profile : {C_BOLD}{version_id}{C_RESET} ({mode.upper()})")
    log_info(f"Target  : {C_YELLOW}{target_os.upper()}{C_RESET}")
    print(f"{C_MAGENTA}{'─'*50}{C_RESET}")

    try:
        ver_data = requests.get(url, timeout=15).json()
    except Exception as e:
        log_error(f"Failed to compile version manifests: {e}")
        return

    libs_to_download = []
    assets_to_download = []
    
    include_client = mode in ['full', 'client']
    include_server = mode in ['full', 'server']
    include_libs   = mode in ['full', 'lib']
    include_assets = mode in ['full', 'assets']

    if include_libs:
        for lib in ver_data.get('libraries', []):
            libs_to_download.extend(parse_library(lib, target_os))

    if include_assets and 'assetIndex' in ver_data:
        asset_index_info = ver_data['assetIndex']
        sys.stdout.write(f"{C_CYAN}[ℹ]{C_RESET} Mapping resource arrays... ")
        sys.stdout.flush()
        try:
            asset_index_data = requests.get(asset_index_info['url'], timeout=15).json()
            for rel_path, asset_meta in asset_index_data.get('objects', {}).items():
                a_hash = asset_meta['hash']
                a_url = f"https://resources.download.minecraft.net/{a_hash[:2]}/{a_hash}"
                assets_to_download.append((f"assets/objects/{a_hash[:2]}/{a_hash}", a_url))
            print(f"{C_GREEN}Done.{C_RESET}")
        except Exception as e:
            print(f"{C_RED}Failed.{C_RESET}")
            log_warn(f"Assets mapping suspended: {e}")

    total_steps = (1 if include_client else 0) + (1 if include_server else 0) + len(libs_to_download) + len(assets_to_download)
    
    if total_steps == 0:
        log_warn("No items found matching criteria.")
        return

    log_info(f"Streaming files directly to ZIP archive...")
    
    # Initialize temporal baseline mark for accurate velocity calculation
    start_time = time.time()
    current_step = 0
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        if include_assets and 'assetIndex' in ver_data:
            index_id = ver_data['assetIndex']['id']
            z.writestr(f"assets/indexes/{index_id}.json", json.dumps(asset_index_data, indent=2))
            
        z.writestr(f"versions/{version_id}/{version_id}.json", json.dumps(ver_data, indent=2))
        
        if include_client:
            client_url = ver_data.get('downloads', {}).get('client', {}).get('url')
            if client_url:
                try:
                    current_step += 1
                    draw_progress_bar(current_step, total_steps, start_time, prefix="Client")
                    z.writestr(f"versions/{version_id}/{version_id}.jar", requests.get(client_url, timeout=20).content)
                except Exception:
                    pass

        if include_server:
            server_url = ver_data.get('downloads', {}).get('server', {}).get('url')
            if server_url:
                try:
                    current_step += 1
                    draw_progress_bar(current_step, total_steps, start_time, prefix="Server")
                    z.writestr(f"server/server.jar", requests.get(server_url, timeout=20).content)
                except Exception:
                    pass
            else:
                log_warn(f"No official server binary provided for {version_id}")

        for path, dl_url in libs_to_download:
            current_step += 1
            draw_progress_bar(current_step, total_steps, start_time, prefix="Libs")
            try:
                res = requests.get(dl_url, timeout=15)
                if res.status_code == 200: z.writestr(path, res.content)
            except Exception: continue

        for path, dl_url in assets_to_download:
            current_step += 1
            if current_step % 5 == 0 or current_step == total_steps:
                draw_progress_bar(current_step, total_steps, start_time, prefix="Assets")
            try:
                res = requests.get(dl_url, timeout=10)
                if res.status_code == 200: z.writestr(path, res.content)
            except Exception: continue

    print() 
    log_success(f"Compilation complete.")
    print(f"{C_GREEN}{C_BOLD}>>> Save Location: ./{zip_path}{C_RESET}\n")

def display_banner():
    os.system('clear')
    print(f"{C_CYAN}┌────────────────────────────────────────────────────────┐")
    print(f"│  {C_WHITE}{C_BOLD}MINECRAFT DEPLOYMENT & METADATA RUNTIME ENVIRONMENT{C_RESET}{C_CYAN}  │")
    print(f"└────────────────────────────────────────────────────────┘{C_RESET}")
    print(f" {C_GRAY}Commands: ls | cd <dir> | dl <ver> [mode] | os <arch> | clear | exit{C_RESET}")
    print(f" {C_GRAY}Modes:    full | client | server | lib | assets{C_RESET}\n")

def start_shell():
    sys.stdout.write(f"{C_BLUE}[⚙]{C_RESET} Synchronizing master manifest metadata... ")
    sys.stdout.flush()
    try:
        manifest = requests.get(MANIFEST_URL).json()
        print(f"{C_GREEN}Connected.{C_RESET}")
    except Exception as e:
        print(f"{C_RED}Failed.{C_RESET}")
        log_error(f"Upstream bridge interface failure: {e}")
        return

    categories = {"release": [], "snapshot": [], "old_beta": [], "old_alpha": []}
    version_urls = {}
    
    for v in manifest.get("versions", []):
        v_type = v['type']
        v_id = v['id']
        version_urls[v_id] = v['url']
        if v_type in categories:
            categories[v_type].append(v_id)
        else:
            if "other" not in categories: categories["other"] = []
            categories["other"].append(v_id)

    display_banner()
    current_dir = "/"

    while True:
        prompt = f"{C_BLUE}┌───{C_GRAY}[{C_WHITE}mc-cluster{C_GRAY}]─[{C_GREEN}{shell_state['os']}{C_GRAY}]─[{C_YELLOW}{current_dir}{C_GRAY}]\n{C_BLUE}└───{C_CYAN}$ {C_RESET}"
        try:
            user_input = input(prompt).strip().split()
        except (KeyboardInterrupt, EOFError):
            print("\nTerminating active cluster link.")
            break

        if not user_input:
            continue

        cmd = user_input[0].lower()
        args = user_input[1:]

        if cmd == "exit":
            break
        elif cmd == "clear":
            display_banner()
        elif cmd == "pwd":
            print(f"{C_WHITE}{current_dir}{C_RESET}")
        elif cmd == "os":
            if not args or args[0].lower() not in ['linux', 'windows', 'osx']:
                log_error("Invalid argument. Syntax: os [linux | windows | osx]")
            else:
                shell_state["os"] = args[0].lower()
                log_success(f"Target environment configured to: {C_BOLD}{shell_state['os']}{C_RESET}")
        elif cmd == "cd":
            if not args:
                current_dir = "/"
                continue
            target = args[0]
            if target == "..":
                current_dir = "/"
            elif current_dir == "/":
                if target in categories: current_dir = f"/{target}"
                else: log_error(f"Directory node not resolved: '{target}'")
            else:
                log_warn("Deep file trees are locked. Use 'cd ..' to return to root branch.")
        elif cmd == "ls":
            if current_dir == "/":
                for cat in categories.keys():
                    print(f"  {C_BLUE}📁 {cat}/{C_RESET}")
                print()
            else:
                cat = current_dir.strip("/")
                v_list = categories[cat]
                print(f"{C_GRAY}📋 Records inside registry: {len(v_list)}{C_RESET}")
                for i in range(0, len(v_list), 5):
                    print("  ".join(f"{C_WHITE}{v:<15}{C_RESET}" for v in v_list[i:i+5]))
                print()
        elif cmd == "dl":
            if not args:
                log_error("Missing parameters. Syntax: dl <version_id> [mode]")
                continue
            target_version = args[0]
            mode = args[1].lower() if len(args) > 1 else "full"
            
            if mode not in ['full', 'client', 'server', 'lib', 'assets']:
                log_error(f"Rejected illegal compilation footprint mode: '{mode}'")
                continue
            if target_version not in version_urls:
                log_error(f"Unknown software version: '{target_version}'")
                continue
                
            download_engine(target_version, version_urls[target_version], mode)
        else:
            log_error(f"Unknown runtime operation code: '{cmd}'")

if __name__ == "__main__":
    start_shell()
