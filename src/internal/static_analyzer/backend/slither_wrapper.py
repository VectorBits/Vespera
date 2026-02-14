#!/usr/bin/env python3
"""
//qhello 专门为mode2设计

Slither包装脚本，供Go调用
输入：JSON格式的stdin
输出：JSON格式的stdout

参考了utils_download-main 的实现
"""
import json
import sys
import tempfile
import os
import subprocess
import re
import shutil
from shutil import which

try:
    from slither import Slither
    from crytic_compile import CryticCompile
except ImportError as e:
    print(json.dumps({
        'success': False,
        'error': (
            f'Import error: {e}. Please install: pip install slither-analyzer '
            f'crytic-compile --break-system-packages'
        )
    }))
    sys.exit(1)

# 尝试导入 solcx（可选）
try:
    import solcx
    SOLCX_AVAILABLE = True
except ImportError:
    SOLCX_AVAILABLE = False

# 版本缓存，避免重复获取路径
_VERSION_CACHE = {}
_CURRENT_SOLC_SELECT_VERSION = None


def remove_duplicate_interfaces(code):
    pattern = re.compile(
        r'\binterface\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{'
    )
    seen = set()
    i = 0
    out = []
    while True:
        m = pattern.search(code, i)
        if not m:
            out.append(code[i:])
            break
        start = m.start()
        out.append(code[i:start])
        name = m.group(1)
        brace_idx = m.end() - 1
        depth = 0
        j = brace_idx
        while j < len(code):
            ch = code[j]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        if depth != 0:
            out.append(code[start:])
            break
        block = code[start:j]
        if name in seen:
            i = j
            continue
        seen.add(name)
        out.append(block)
        i = j
    return ''.join(out)


def install_solc_version(version):
    """使用 solcx 安装 Solidity 版本（如果未安装）"""
    if not version or not SOLCX_AVAILABLE:
        return None

    try:
        # 标准化版本格式
        version_normalized = version.lstrip('v') if version.startswith('v') else version
        version_str = f'v{version_normalized}'  # solcx.install_solc 需要 v 前缀
        
        # 检查版本是否已安装
        installed_versions = solcx.get_installed_solc_versions()
        install_folder = solcx.get_solcx_install_folder()
        for installed_version in installed_versions:
            installed_str = str(installed_version).lstrip('v')
            if installed_str == version_normalized:
                # 版本已安装，构建路径
                possible_paths = [
                    os.path.join(install_folder, f"solc-v{installed_str}"),
                    os.path.join(install_folder, f"solc-v{installed_str}", "solc"),
                    os.path.join(install_folder, f"solc-{installed_str}"),
                ]
                for path in possible_paths:
                    if os.path.exists(path) and os.access(path, os.X_OK):
                        return path
                return None

        # 版本未安装，尝试安装
        print(f"Installing solc {version_normalized}...", file=sys.stderr)
        solcx.install_solc(version_str)
        # 安装后再次查找
        installed_versions = solcx.get_installed_solc_versions()
        install_folder = solcx.get_solcx_install_folder()
        for installed_version in installed_versions:
            installed_str = str(installed_version).lstrip('v')
            if installed_str == version_normalized:
                # 构建路径
                possible_paths = [
                    os.path.join(install_folder, f"solc-v{installed_str}"),
                    os.path.join(install_folder, f"solc-v{installed_str}", "solc"),
                    os.path.join(install_folder, f"solc-{installed_str}"),
                ]
                for path in possible_paths:
                    if os.path.exists(path) and os.access(path, os.X_OK):
                        return path
        return None
    except Exception as e:
        print(f"Warning: solcx install failed: {e}", file=sys.stderr)
        # 如果 solcx 安装失败，尝试使用 solc-select
        return try_solc_select(version)


def try_solc_select(version):
    """尝试使用 solc-select 安装和切换版本"""
    global _CURRENT_SOLC_SELECT_VERSION

    if not version:
        return None

    try:
        # 检查 solc-select 是否可用
        if not which('solc-select'):
            return None

        # 检查版本是否已安装
        list_result = subprocess.run(
            ['solc-select', 'versions'],
            capture_output=True, text=True, timeout=10
        )
        version_installed = False
        if list_result.returncode == 0:
            installed_versions = list_result.stdout.strip().split('\n')
            version_installed = version in installed_versions

        # 如果未安装，尝试安装版本
        if not version_installed:
            install_result = subprocess.run(
                ['solc-select', 'install', version],
                capture_output=True, text=True, timeout=60
            )
            if install_result.returncode != 0:
                # 安装失败，但继续尝试切换（可能已经安装了）
                pass

        # 只在版本不同时才切换（优化性能）
        if _CURRENT_SOLC_SELECT_VERSION != version:
            use_result = subprocess.run(
                ['solc-select', 'use', version],
                capture_output=True, text=True, timeout=10
            )

            if use_result.returncode == 0:
                _CURRENT_SOLC_SELECT_VERSION = version
                # 调试信息输出到 stderr，不会影响 JSON 输出
                print(f"[DEBUG] Switched to solc {version}", file=sys.stderr)
            else:
                return None

        # 返回 solc 路径
        result = subprocess.run(
            ['which', 'solc'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()

        return None
    except (subprocess.TimeoutExpired, FileNotFoundError,
            subprocess.SubprocessError):
        return None


def get_solc_path(version):
    """获取 solc 编译器路径（带缓存）"""
    if not version:
        return None

    # 检查缓存
    if version in _VERSION_CACHE:
        cached_path = _VERSION_CACHE[version]
        # 验证路径是否仍然有效
        if cached_path and os.path.exists(cached_path):
            return cached_path

    solc_path = None

    # 方法1: 使用 solcx（如果可用，推荐，不需要全局切换）
    if SOLCX_AVAILABLE:
        try:
            installed_versions = solcx.get_installed_solc_versions()
            
            # 标准化版本格式：移除 v 前缀（solcx 使用不带 v 的格式）
            version_normalized = version.lstrip('v') if version.startswith('v') else version
            
            # 查找匹配的版本（solcx 返回的版本对象需要转换为字符串比较）
            for installed_version in installed_versions:
                installed_str = str(installed_version)
                # 移除可能的 v 前缀进行比较
                installed_normalized = installed_str.lstrip('v')
                
                if installed_normalized == version_normalized:
                    # solcx 没有 get_executable，需要手动构建路径
                    install_folder = solcx.get_solcx_install_folder()
                    # 尝试不同的路径格式
                    possible_paths = [
                        os.path.join(install_folder, f"solc-v{installed_str}"),
                        os.path.join(install_folder, f"solc-v{installed_str}", "solc"),
                        os.path.join(install_folder, f"solc-{installed_str}"),
                    ]
                    for path in possible_paths:
                        if os.path.exists(path) and os.access(path, os.X_OK):
                            _VERSION_CACHE[version] = path
                            return path
        except Exception as e:
            print(f"Warning: solcx error: {e}", file=sys.stderr)
            pass

    # 方法2: 尝试安装（使用 solcx）
    if not solc_path:
        solc_path = install_solc_version(version)
        if solc_path and os.path.exists(solc_path):
            _VERSION_CACHE[version] = solc_path
            return solc_path

    # 方法3: 使用 solc-select（需要全局切换）
    if not solc_path:
        solc_path = try_solc_select(version)
        if solc_path and os.path.exists(solc_path):
            _VERSION_CACHE[version] = solc_path
            return solc_path

    return None


def analyze_contract(code, config):
    """分析合约代码"""
    # 根据输入自动判断是单文件源码还是 Etherscan 多文件 JSON
    temp_file = None
    temp_dir = None
    foundry_dir = None

    # 从 config 中获取合约地址，用于命名扁平化文件
    import uuid
    import time
    contract_address = config.get('address')
    if not contract_address:
        # 如果没有地址，生成一个随机文件名
        contract_address = f"unknown_{uuid.uuid4().hex[:8]}"

    def _cleanup():
        if temp_file and os.path.exists(temp_file):
            try:
                os.unlink(temp_file)
            except Exception:
                pass
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
        if foundry_dir and os.path.exists(foundry_dir):
            try:
                shutil.rmtree(foundry_dir)
            except Exception:
                pass

    try:
        # 获取 Solidity 版本（来自上游 Go 端的推断）
        solc_version = config.get('solc_version', '')
        
        # 如果 code 看起来像 JSON（Etherscan 多文件格式），尝试解析并落盘多文件
        target_path = None
        sources = None
        settings = {}
        if isinstance(code, str) and code.strip().startswith("{"):
            parsed = None
            raw = code.strip()
            # 有些 Etherscan 返回会外层再包一层字符串或带多余括号，尽量容错解析
            for candidate in (raw, raw[1:-1] if (raw.startswith("{") and raw.endswith("}")) else raw):
                try:
                    parsed = json.loads(candidate)
                    break
                except Exception:
                    continue
            # Etherscan 两种常见结构：{"sources": {...}} 或 直接 {<path>: {"content": "..."}}
            if isinstance(parsed, dict):
                if "sources" in parsed and isinstance(parsed["sources"], dict):
                    sources = parsed["sources"]
                    if "settings" in parsed and isinstance(parsed["settings"], dict):
                        settings = parsed["settings"]
                else:
                    # 尝试把字典直接当作 sources
                    # 需要确保每个 value 至少包含 "content"
                    if all(isinstance(v, dict) and "content" in v for v in parsed.values()):
                        sources = parsed

        # 如果有多文件 sources，写入临时目录
        if sources:
            temp_dir = tempfile.mkdtemp(prefix="slither_multi_")
            for rel_path, meta in sources.items():
                rel_path = rel_path.lstrip("./").strip()
                if not rel_path.endswith(".sol"):
                    # 给没有后缀的补 .sol，避免异常
                    rel_path = rel_path + ".sol"
                abs_path = os.path.join(temp_dir, rel_path)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                content = meta.get("content", "")
                # 统一行结尾
                content = content.replace("\r\n", "\n")
                with open(abs_path, "w", encoding="utf-8") as fw:
                    fw.write(content)
            target_path = temp_dir

            # 如果 Go 端没有传版本，尝试在所有 sources 中提取 pragma 版本
            if not solc_version:
                version_re = re.compile(r"pragma\s+solidity\s+([^;]+);", re.IGNORECASE)
                version_full = re.compile(r"(\d+\.\d+\.\d+)")
                detected = []
                for rel_path, meta in sources.items():
                    content = meta.get("content", "")
                    m = version_re.search(content)
                    if not m:
                        continue
                    vseg = m.group(1)
                    m2 = version_full.search(vseg)
                    if m2:
                        detected.append(m2.group(1))
                # 简单选择出现次数最多的版本
                if detected:
                    counts = {}
                    for v in detected:
                        counts[v] = counts.get(v, 0) + 1
                    solc_version = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

            # 优先尝试使用 Foundry 扁平化（参考 verify_code.py 的实现）
            flattened_file = None
            if which("forge") is not None:
                try:
                    print(f"[DEBUG] 尝试使用 Forge 扁平化合约...", file=sys.stderr)
                    # 在新的 foundry 工程目录内进行
                    foundry_dir = tempfile.mkdtemp(prefix="slither_foundry_")
                    
                    # 1. 将多文件源码复制到 foundry_dir 下（保持原目录结构）
                    # 参考 utils_download-main/verify_code.py 的 export_multifile 函数
                    src_paths = []  # 记录所有源文件路径
                    
                    for rel_path, meta in sources.items():
                        rel = rel_path.lstrip("./").strip()
                        if not rel.endswith(".sol"):
                            rel = rel + ".sol"
                        abs_path = os.path.join(foundry_dir, rel)
                        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                        
                        content = meta.get("content", "")
                        with open(abs_path, "w", encoding="utf-8") as fw:
                            fw.write(content.replace("\r\n", "\n"))
                        src_paths.append(rel)
                    
                    # 2. 初始化 Foundry 项目（参考 utils_download-main/verify_code.py）
                    print(f"[DEBUG] 初始化 Foundry 项目...", file=sys.stderr)
                    init_result = subprocess.run(
                        ["forge", "init", ".", "--force", "--no-git"],
                        cwd=foundry_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=30
                    )
                    if init_result.returncode != 0:
                        print(f"[DEBUG] forge init 失败: {init_result.stderr}", file=sys.stderr)
                        raise Exception("forge init failed")
                    
                    # 2.5. 删除 Forge 默认的示例文件（避免版本冲突）
                    print(f"[DEBUG] 清理 Forge 默认文件...", file=sys.stderr)
                    default_files_to_remove = [
                        os.path.join(foundry_dir, "script", "Counter.s.sol"),
                        os.path.join(foundry_dir, "src", "Counter.sol"),
                        os.path.join(foundry_dir, "test", "Counter.t.sol"),
                    ]
                    for file_path in default_files_to_remove:
                        if os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                            except Exception:
                                pass
                    
                    # 3. 配置 foundry.toml（参考 verify_code.py 的实现）
                    toml_path = os.path.join(foundry_dir, "foundry.toml")
                    if os.path.exists(toml_path):
                        with open(toml_path, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                        
                        # 清理并重写配置
                        new_lines = []
                        remapping_flag = False
                        for line in lines:
                            # 跳过已有的 remappings 和注释
                            if line.startswith('remappings =') and settings.get("remappings"):
                                remapping_flag = True
                                continue
                            if remapping_flag:
                                if line.strip().endswith(']'):
                                    remapping_flag = False
                                continue
                            if line.startswith('# See more config options'):
                                continue
                            # 保留其他配置
                            if line.strip() != '':
                                new_lines.append(line)
                        
                        # 添加自定义配置（只使用 settings 中的 remappings）
                        remappings = settings.get("remappings", [])
                        if remappings:
                            new_lines.append('remappings = [\n')
                            # 添加用户定义的 remappings
                            for r in remappings:
                                new_lines.append(f'    "{r}",\n')
                            new_lines.append(']\n')
                        
                        # 添加 Solidity 版本
                        # 检查版本兼容性（Forge 不支持 0.9.0 等版本）
                        if solc_version:
                            # 解析版本号（处理可能的版本格式：0.8.1, ^0.8.1, >=0.8.1 等）
                            version_str = solc_version.strip()
                            # 移除版本前缀符号
                            for prefix in ['^', '>=', '<=', '>', '<', '~', '=']:
                                if version_str.startswith(prefix):
                                    version_str = version_str[len(prefix):].strip()
                            
                            # 解析版本号
                            try:
                                version_parts = version_str.split('.')
                                if len(version_parts) >= 2:
                                    major = int(version_parts[0])
                                    minor = int(version_parts[1])
                                    # Forge 不支持 0.9.0，使用 0.8.x 的最新版本
                                    if major == 0 and minor >= 9:
                                        print(f"[DEBUG] Solidity {solc_version} 不被 Forge 支持，使用 0.8.26 替代", file=sys.stderr)
                                        solc_version = "0.8.26"
                            except (ValueError, IndexError):
                                # 版本解析失败，使用原版本
                                print(f"[DEBUG] 无法解析版本号 {solc_version}，使用原版本", file=sys.stderr)
                            
                            new_lines.append(f'solc_version = "{solc_version}"\n')
                        
                        # 添加优化配置
                        via_ir = settings.get("viaIR", False)
                        if via_ir:
                            new_lines.append('via_ir = true\n')
                            new_lines.append('optimizer = true\n')
                            new_lines.append('optimizer_runs = 200\n')
                        
                        # 添加 EVM 版本
                        evm_version = settings.get("evmVersion")
                        if evm_version:
                            new_lines.append(f'evm_version = "{evm_version}"\n')
                        
                        with open(toml_path, "w", encoding="utf-8") as f:
                            f.writelines(new_lines)
                    
                    # 4. 编译合约（参考 verify_code.py，不使用 forge install）
                    print(f"[DEBUG] 编译合约...", file=sys.stderr)
                    build_result = subprocess.run(
                        ["forge", "build"],
                        cwd=foundry_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=120
                    )
                    
                    if build_result.returncode != 0:
                        print(f"[DEBUG] forge build 失败: {build_result.stderr}", file=sys.stderr)
                        raise Exception("forge build failed")
                    
                    print(f"[DEBUG] 编译成功！", file=sys.stderr)
                    
                    # 6. 选择主合约文件进行扁平化（排除依赖库，选择最大的用户合约）
                    candidates = []
                    
                    # 扩展的依赖库排除列表
                    excluded_patterns = [
                        'node_modules/', 'lib/', '@openzeppelin/', '@chainlink/', 'forge-std/',
                        'erc721a/', 'erc20/', 'erc1155/', 'erc777/',  # 常见的标准库
                        'contracts/interfaces/', 'interfaces/',  # 接口文件夹
                        '/test/', '/tests/', '/mock/', '/mocks/',  # 测试文件
                    ]
                    
                    for src_path in src_paths:
                        # 排除依赖库目录
                        if any(excluded in src_path.lower() for excluded in excluded_patterns):
                            print(f"[DEBUG] 排除依赖/测试文件: {src_path}", file=sys.stderr)
                            continue
                        
                        fp = os.path.join(foundry_dir, src_path)
                        if os.path.exists(fp):
                            try:
                                size = os.path.getsize(fp)
                                candidates.append((size, src_path))
                                print(f"[DEBUG] 候选合约: {src_path} (大小: {size})", file=sys.stderr)
                            except Exception:
                                pass
                    
                    if not candidates:
                        # 如果没有找到用户合约，尝试所有文件（回退）
                        print(f"[DEBUG] 未找到用户合约，尝试所有文件...", file=sys.stderr)
                        for src_path in src_paths:
                            fp = os.path.join(foundry_dir, src_path)
                            if os.path.exists(fp):
                                try:
                                    size = os.path.getsize(fp)
                                    candidates.append((size, src_path))
                                except Exception:
                                    pass
                    
                    if not candidates:
                        raise Exception("No valid source files found")
                    
                    candidates.sort(reverse=True)
                    main_contract_path = candidates[0][1]
                    print(f"[DEBUG] 选择主合约: {main_contract_path}", file=sys.stderr)
                    
                    # 5. 扁平化合约（完全参考 utils_download-main/verify_code.py）
                    flattened_file = os.path.join(foundry_dir, "flattened.sol")
                    
                    # 确定 pragma 版本
                    if solc_version:
                        pragma_version = solc_version
                    else:
                        pragma_version = "0.8.0"
                    
                    print(f"[DEBUG] 执行 forge flatten...", file=sys.stderr)

                    flatten_result = subprocess.run(
                        ["forge", "flatten", main_contract_path],
                        cwd=foundry_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=60
                    )

                    if flatten_result.returncode == 0 and flatten_result.stdout and flatten_result.stdout.strip():
                        flattened_lines_raw = flatten_result.stdout.splitlines()
                        flattened_lines_raw = [
                            line for line in flattened_lines_raw
                            if "SPDX-License-Identifier" not in line and "pragma solidity" not in line
                        ]
                        flattened_content = "\n".join([
                            "// SPDX-License-Identifier: MIT",
                            f"pragma solidity ^{pragma_version};",
                            *flattened_lines_raw,
                            ""
                        ])

                        with open(flattened_file, "w", encoding="utf-8") as f:
                            f.write(flattened_content)
                        
                        if len(flattened_content.strip()) > 100:  # 确保文件有实质内容
                            # 根据用户要求，删除所有注释和文档标签
                            print(f"[DEBUG] 正在删除所有注释...", file=sys.stderr)

                            lines = flattened_content.split('\n')
                            
                            # 前两行是 sed 添加的 SPDX 和 pragma，保留它们
                            header_lines = lines[:2]
                            content_to_clean = '\n'.join(lines[2:])

                            # 1. 删除多行注释 /* ... */
                            no_multiline_comments = re.sub(r'/\*[\s\S]*?\*/', '', content_to_clean, flags=re.MULTILINE)
                            
                            # 2. 删除单行注释 // ...
                            no_single_line_comments = re.sub(r'//.*', '', no_multiline_comments)
                            
                            # 3. 移除因删除注释而产生的多余空行
                            cleaned_lines = [line for line in no_single_line_comments.split('\n') if line.strip() != '']
                            
                            # 重新组合文件内容
                            final_lines = header_lines + cleaned_lines
                            flattened_content = '\n'.join(final_lines)
                            flattened_content = remove_duplicate_interfaces(flattened_content)

                            print(f"[DEBUG] 所有注释已删除。", file=sys.stderr)
                            
                            # 写回清理后的内容
                            with open(flattened_file, "w", encoding="utf-8") as f:
                                f.write(flattened_content)
                            
                            # 将扁平化文件保存到项目目录的 flattened_contracts/ 文件夹
                            try:
                                # os.getcwd() 对于从Go调用时的路径可能不准确
                                # 使用脚本自身的路径来定位项目根目录
                                # 脚本路径: src/internal/static_analyzer/backend/slither_wrapper.py
                                script_dir = os.path.dirname(os.path.abspath(__file__))
                                # 回退4层到项目根目录
                                project_root = os.path.abspath(os.path.join(script_dir, '../../../../'))
                                output_dir = os.path.join(project_root, 'flattened_contracts')
                                os.makedirs(output_dir, exist_ok=True)
                                
                                # 生成文件名，处理冲突
                                base_filename = contract_address if contract_address else f"unknown_{uuid.uuid4().hex[:8]}"
                                target_filename = f"{base_filename}.sol"
                                final_path = os.path.join(output_dir, target_filename)
                                
                                # 如果文件已存在，添加时间戳后缀避免覆盖
                                if os.path.exists(final_path):
                                    timestamp = int(time.time())
                                    base_name = base_filename
                                    target_filename = f"{base_name}_{timestamp}.sol"
                                    final_path = os.path.join(output_dir, target_filename)
                                
                                shutil.copyfile(flattened_file, final_path)
                                print(f"[DEBUG] 扁平化文件已保存到: {final_path}", file=sys.stderr)
                            except Exception as e:
                                print(f"[DEBUG] 保存扁平化文件失败: {e}", file=sys.stderr)
                                import traceback
                                print(f"[DEBUG] 错误详情: {traceback.format_exc()}", file=sys.stderr)

                            # 验证清理结果
                            pragma_count = flattened_content.count('pragma solidity')
                            spdx_count = flattened_content.count('SPDX-License-Identifier')
                            
                            print(f"[DEBUG] forge flatten 成功！", file=sys.stderr)
                            print(f"[DEBUG] 扁平化文件: {flattened_file}", file=sys.stderr)
                            print(f"[DEBUG] pragma 数量: {pragma_count}, SPDX 数量: {spdx_count}", file=sys.stderr)
                            
                            # 使用扁平化文件
                            target_path = flattened_file
                        else:
                            print(f"[DEBUG] 扁平化文件内容为空，放弃扁平化", file=sys.stderr)
                            raise Exception("flattened file is empty")
                    else:
                        # forge flatten 失败
                        error_msg = flatten_result.stderr.strip()
                        if len(error_msg) > 200:
                            error_msg = error_msg[:200] + "..."
                        print(f"[DEBUG] forge flatten 失败: {error_msg}", file=sys.stderr)
                        raise Exception("forge flatten failed")
                        
                except subprocess.TimeoutExpired:
                    print(f"[DEBUG] Forge 操作超时，放弃扁平化", file=sys.stderr)
                    # 直接返回错误，不使用多文件回退（Slither 对多文件支持差）
                    result = {
                        "success": False,
                        "error": "Forge flatten timeout. 合约结构过于复杂，无法扁平化。"
                    }
                    print(json.dumps(result))
                    sys.exit(1)
                except Exception as forge_err:
                    error_msg = str(forge_err)
                    if len(error_msg) > 200:
                        error_msg = error_msg[:200] + "..."
                    print(f"[DEBUG] Forge 扁平化失败: {error_msg}", file=sys.stderr)
                    # 直接返回错误，不使用多文件回退（Slither 对多文件支持差）
                    result = {
                        "success": False,
                        "error": f"Forge flatten failed: {error_msg}"
                    }
                    print(json.dumps(result))
                    sys.exit(1)
            else:
                # 未检测到 forge，无法处理多文件合约
                print(f"[DEBUG] 未检测到 forge，无法处理多文件合约", file=sys.stderr)
                result = {
                    "success": False,
                    "error": "Forge not found. Cannot process multi-file contracts without Forge."
                }
                print(json.dumps(result))
                sys.exit(1)
        else:
            # 单文件：落盘到临时文件
            with tempfile.NamedTemporaryFile(mode='w', suffix='.sol', delete=False) as f:
                f.write(code)
                temp_file = f.name
            target_path = temp_file

        # 解析/安装 solc 路径
        solc_path = None
        if solc_version:
            print(f"Getting solc path for version {solc_version}...", file=sys.stderr)
            try:
                solc_path = get_solc_path(solc_version)
                if solc_path and os.path.exists(solc_path):
                    print(f"Using solc at: {solc_path}", file=sys.stderr)
                else:
                    print(f"Warning: Could not get solc path for {solc_version}, will try auto-detection", file=sys.stderr)
                    solc_path = None
            except Exception as e:
                print(f"Warning: Error getting solc path for {solc_version}: {e}, will try auto-detection", file=sys.stderr)
                solc_path = None
        
        # 编译配置
        solc_args = ""
        if config.get('optimization'):
            solc_args += "--optimize "
        if config.get('via_ir'):
            solc_args += "--via-ir "
        solc_args = solc_args.strip()
        
        # 使用 CryticCompile 编译（支持目录或单文件；若已扁平化，则 target_path 为单文件）
        try:
            if solc_path:
                crytic_compile = CryticCompile(
                    target=target_path,
                    solc=solc_path,
                    solc_args=solc_args if solc_args else None
                )
            else:
                crytic_compile = CryticCompile(
                    target=target_path,
                    solc_version=solc_version,
                    solc_args=solc_args if solc_args else None
                )

            slither = Slither(crytic_compile)
        except Exception as cc_err:
            # 回退逻辑：若目标是目录，收集所有 .sol 文件再交给 Slither
            print(f"Warning: CryticCompile failed, fallback to raw Slither: {cc_err}", file=sys.stderr)
            if os.path.isdir(target_path):
                # 选择一个可能的主文件（最大体积）
                sol_files = []
                for root, _, files in os.walk(target_path):
                    for name in files:
                        if name.endswith(".sol"):
                            fp = os.path.join(root, name)
                            try:
                                size = os.path.getsize(fp)
                            except Exception:
                                size = 0
                            sol_files.append((size, fp))
                if not sol_files:
                    raise Exception(f"Invalid compilation: {target_path} is a directory with no .sol files")
                sol_files.sort(reverse=True)
                main_file = sol_files[0][1]
                if solc_path:
                    slither = Slither(main_file, solc=solc_path)
                else:
                    slither = Slither(main_file)
            else:
                # 单文件回退
                if solc_path:
                    slither = Slither(target_path, solc=solc_path)
                else:
                    slither = Slither(target_path)
        
        # 提取信息
        result = {
            'state_variables': [],
            'functions': [],
            'detectors': []
        }
        
        # 获取主合约
        contracts = slither.contracts
        if contracts:
            main_contract = contracts[0]
            
            # 提取状态变量
            for var in main_contract.state_variables:
                result['state_variables'].append({
                    'name': var.name,
                    'type': str(var.type),
                    'visibility': var.visibility,
                    'is_constant': var.is_constant
                })
            
            # 提取函数
            for func in main_contract.functions:
                if func.is_constructor or func.is_fallback or func.is_receive:
                    continue
                
                # 获取参数类型
                params = []
                if func.parameters:
                    params = [str(p.type) for p in func.parameters]
                
                # 获取返回类型
                returns = []
                if func.returns:
                    returns = [str(r.type) for r in func.returns]
                
                # 获取 state_mutability（旧版本可能没有这个属性）
                state_mutability = 'nonpayable'  # 默认值
                if hasattr(func, 'state_mutability'):
                    state_mutability = func.state_mutability
                elif hasattr(func, 'payable'):
                    # 旧版本使用 payable 属性
                    state_mutability = 'payable' if func.payable else 'nonpayable'
                
                result['functions'].append({
                    'name': func.name,
                    'signature': func.signature_str if hasattr(func, 'signature_str') else func.name,
                    'visibility': func.visibility,
                    'state_mutability': state_mutability,
                    'parameters': params,
                    'returns': returns
                })
            
            # 运行检测器并获取结果
            try:
                # 运行检测器
                slither.run_detectors()
                
                # 方法1: 使用 Slither 的 detectors 属性（如果可用）
                if hasattr(slither, 'detectors') and slither.detectors:
                    for detector in slither.detectors:
                        if hasattr(detector, 'results') and detector.results:
                            for finding in detector.results:
                                # 获取检测器名称
                                check_name = 'unknown'
                                if hasattr(detector, 'ARGUMENT'):
                                    check_name = detector.ARGUMENT
                                elif hasattr(detector, '__class__'):
                                    check_name = detector.__class__.__name__.replace('Detector', '').lower()
                                
                                # 提取影响和置信度
                                impact = 'Unknown'
                                confidence = 'Unknown'
                                if hasattr(finding, 'impact'):
                                    impact = str(finding.impact)
                                if hasattr(finding, 'confidence'):
                                    confidence = str(finding.confidence)
                                
                                # 过滤低风险漏洞 (Low/Informational)
                                if impact.lower() in ['low', 'informational', 'optimization']:
                                    continue
                                
                                # 提取行号
                                line_numbers = []
                                if hasattr(finding, 'elements'):
                                    for element in finding.elements:
                                        if hasattr(element, 'source_mapping'):
                                            source_mapping = element.source_mapping
                                            if source_mapping and 'lines' in source_mapping:
                                                line_numbers.extend(source_mapping['lines'])
                                
                                # 去重并排序行号
                                line_numbers = sorted(list(set(line_numbers)))

                                result['detectors'].append({
                                    'check': check_name,
                                    'impact': impact,
                                    'confidence': confidence,
                                    'description': str(finding),
                                    'line_numbers': line_numbers
                                })
                
                # 方法2: 手动运行所有检测器（如果 detectors 属性为空）
                if len(result['detectors']) == 0:
                    try:
                        # 导入检测器模块
                        import slither.detectors as detectors_module
                        import inspect
                        
                        # 获取所有检测器类
                        detector_classes = []
                        for name in dir(detectors_module):
                            obj = getattr(detectors_module, name)
                            # 检查是否是检测器类
                            if (inspect.isclass(obj) and 
                                hasattr(obj, 'detect') and 
                                hasattr(obj, 'ARGUMENT')):
                                detector_classes.append(obj)
                        
                        # 如果上面没找到，尝试从子模块导入
                        if not detector_classes:
                            # 尝试导入常见的检测器模块
                            detector_modules = [
                                'slither.detectors.reentrancy',
                                'slither.detectors.unchecked_transfer',
                                'slither.detectors.uninitialized_state',
                                'slither.detectors.uninitialized_storage',
                                'slither.detectors.arbitrary_send',
                                'slither.detectors.deprecated_standards',
                                'slither.detectors.erc20_indexed',
                                'slither.detectors.incorrect_erc20_interface',
                                'slither.detectors.locked_ether',
                                'slither.detectors.missing_events',
                                'slither.detectors.naming_convention',
                                'slither.detectors.controlled_delegatecall',
                                'slither.detectors.functions_that_send_ether',
                                'slither.detectors.suicidal',
                                'slither.detectors.uninitialized_storage_pointer',
                                'slither.detectors.unused_state_variable',
                                'slither.detectors.timestamp',
                                'slither.detectors.assembly',
                                'slither.detectors.bad_prng',
                                'slither.detectors.boolean_constant',
                                'slither.detectors.constant_pragma',
                                'slither.detectors.deprecated_standards',
                                'slither.detectors.erc20_indexed',
                                'slither.detectors.incorrect_erc20_interface',
                                'slither.detectors.locked_ether',
                                'slither.detectors.missing_events',
                                'slither.detectors.naming_convention',
                                'slither.detectors.controlled_delegatecall',
                                'slither.detectors.functions_that_send_ether',
                                'slither.detectors.suicidal',
                                'slither.detectors.uninitialized_storage_pointer',
                                'slither.detectors.unused_state_variable',
                            ]
                            
                            for mod_name in detector_modules:
                                try:
                                    mod = __import__(mod_name, fromlist=[''])
                                    for name in dir(mod):
                                        obj = getattr(mod, name)
                                        if (inspect.isclass(obj) and 
                                            hasattr(obj, 'detect') and 
                                            hasattr(obj, 'ARGUMENT')):
                                            detector_classes.append(obj)
                                except Exception:
                                    pass
                        
                        # 遍历所有检测器类并运行
                        for detector_class in detector_classes:
                            try:
                                # 实例化检测器
                                detector = detector_class(slither, None)
                                # 运行检测
                                detector.detect()
                                
                                # 获取结果
                                if hasattr(detector, 'results') and detector.results:
                                    # 获取检测器名称
                                    check_name = 'unknown'
                                    if hasattr(detector, 'ARGUMENT'):
                                        check_name = detector.ARGUMENT
                                    elif hasattr(detector_class, '__name__'):
                                        check_name = detector_class.__name__.replace('Detector', '').lower()
                                    
                                    # 处理每个检测结果
                                    for finding in detector.results:
                                        # 提取影响和置信度
                                        impact = 'Unknown'
                                        confidence = 'Unknown'
                                        if hasattr(finding, 'impact'):
                                            impact = str(finding.impact)
                                        if hasattr(finding, 'confidence'):
                                            confidence = str(finding.confidence)
                                        
                                        # 过滤低风险漏洞 (Low/Informational)
                                        if impact.lower() in ['low', 'informational', 'optimization']:
                                            continue

                                        # 提取行号
                                        line_numbers = []
                                        if hasattr(finding, 'elements'):
                                            for element in finding.elements:
                                                if hasattr(element, 'source_mapping'):
                                                    source_mapping = element.source_mapping
                                                    if source_mapping and 'lines' in source_mapping:
                                                        line_numbers.extend(source_mapping['lines'])
                                        
                                        line_numbers = sorted(list(set(line_numbers)))

                                        result['detectors'].append({
                                            'check': check_name,
                                            'impact': impact,
                                            'confidence': confidence,
                                            'description': str(finding),
                                            'line_numbers': line_numbers
                                        })
                            except Exception:
                                # 单个检测器失败不影响其他检测器
                                pass
                    except Exception as import_err:
                        print(f"Warning: Could not import detectors: {import_err}", file=sys.stderr)
                        import traceback
                        print(f"Traceback: {traceback.format_exc()}", file=sys.stderr)
                
                # 方法3: 如果 Python API 没有返回结果，使用命令行工具作为回退
                if len(result['detectors']) == 0:
                    try:
                        print(f"[DEBUG] Python API 未返回检测结果，尝试使用命令行工具...", file=sys.stderr)
                        # 使用 slither 命令行工具获取 JSON 输出
                        slither_cmd = ['slither', target_path, '--json', '-']
                        # 注意：slither 命令行工具使用 --solc 参数指定编译器版本
                        if solc_version:
                            # 先尝试获取 solc 路径
                            solc_path_for_cmd = get_solc_path(solc_version)
                            if solc_path_for_cmd and os.path.exists(solc_path_for_cmd):
                                slither_cmd.extend(['--solc', solc_path_for_cmd])
                            else:
                                # 如果无法获取路径，尝试直接使用版本号（某些 slither 版本支持）
                                slither_cmd.extend(['--solc-version', solc_version])
                        
                        slither_result = subprocess.run(
                            slither_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            timeout=120
                        )
                        
                        # Slither 即使检测到问题也可能返回非零退出码，但 JSON 输出仍然有效
                        # 检查 stdout 是否包含有效的 JSON
                        if slither_result.stdout and slither_result.stdout.strip().startswith('{'):
                            # 解析 JSON 输出
                            try:
                                slither_json = json.loads(slither_result.stdout)
                                detectors_data = slither_json.get('results', {}).get('detectors', [])
                                
                                for detector_data in detectors_data:
                                    impact = detector_data.get('impact', 'Unknown')
                                    
                                    # 过滤低风险漏洞 (Low/Informational)
                                    if impact.lower() in ['low', 'informational', 'optimization']:
                                        continue
                                        
                                    # 提取行号 (从 elements 中)
                                    line_numbers = []
                                    elements = detector_data.get('elements', [])
                                    for element in elements:
                                        source_mapping = element.get('source_mapping', {})
                                        lines = source_mapping.get('lines', [])
                                        line_numbers.extend(lines)
                                    
                                    line_numbers = sorted(list(set(line_numbers)))
                                    
                                    result['detectors'].append({
                                        'check': detector_data.get('check', 'unknown'),
                                        'impact': impact,
                                        'confidence': detector_data.get('confidence', 'Unknown'),
                                        'description': detector_data.get('description', ''),
                                        'line_numbers': line_numbers
                                    })
                                
                                print(f"[DEBUG] 从命令行工具获取到 {len(detectors_data)} 个检测结果", file=sys.stderr)
                            except json.JSONDecodeError as json_err:
                                print(f"[DEBUG] 解析 Slither JSON 输出失败: {json_err}", file=sys.stderr)
                        else:
                            print(f"[DEBUG] Slither 命令行工具执行失败: {slither_result.stderr}", file=sys.stderr)
                    except Exception as cmd_err:
                        print(f"[DEBUG] 使用命令行工具回退失败: {cmd_err}", file=sys.stderr)
                        
            except Exception as e:
                # 检测器失败不影响其他结果，但输出错误信息到 stderr
                print(f"Warning: Detector execution failed: {e}", file=sys.stderr)
                import traceback
                print(f"Traceback: {traceback.format_exc()}", file=sys.stderr)
        
        return result

    except Exception as e:
        error_msg = str(e)
        
        # 检测常见的旧版本语法问题，提供友好的错误信息
        if "constructor()" in error_msg and "Expected identifier" in error_msg:
            error_msg += (
                "\n提示: Solidity 0.4.x 不支持 constructor() 语法。"
                "0.4.x 使用与合约同名的函数作为构造函数。"
                "这可能是合约代码本身的问题，而不是工具问题。"
            )
        elif "emit" in error_msg and "Expected token Semicolon" in error_msg:
            error_msg += (
                "\n提示: emit 关键字在 Solidity 0.4.21 之前不存在。"
                "这可能是合约代码本身的问题，而不是工具问题。"
            )
        elif "Invalid compilation" in error_msg:
            # 提取版本信息
            version = config.get('solc_version', 'unknown')
            error_msg += (
                f"\n提示: 合约代码可能包含与 Solidity {version} 不兼容的语法。"
                "这可能是合约代码本身的问题，而不是工具问题。"
            )
        
        raise Exception(f"Slither analysis failed: {error_msg}")
    finally:
        _cleanup()


if __name__ == '__main__':
    # 从stdin读取JSON输入
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(json.dumps({
            'success': False,
            'error': f'Invalid JSON input: {str(e)}'
        }))
        sys.exit(1)
    
    code = input_data.get('code', '')
    if not code:
        print(json.dumps({
            'success': False,
            'error': 'No code provided'
        }))
        sys.exit(1)
    
    config = input_data.get('config', {})
    
    try:
        result = analyze_contract(code, config)
        print(json.dumps({
            'success': True,
            'result': result
        }, indent=2))
    except Exception as e:
        print(json.dumps({
            'success': False,
            'error': str(e)
        }))
        sys.exit(1)
