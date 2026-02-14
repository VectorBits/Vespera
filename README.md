<div align="center">

# 🛡️ Vespera

AI Agent 驱动的 EVM 智能合约漏洞批量检测框架，定向扫描与混合模糊扫描，面向研究与工程化审计场景。

[![Go Version](https://img.shields.io/badge/Go-1.21+-00ADD8?style=flat&logo=go)](https://golang.org/)
[![License](https://img.shields.io/badge/Apache-License-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-EVM-blue)](https://ethereum.org/)

</div>

## 📘 文档

完整文档请访问：

- https://vespera-doc.vectorbits.net/

## ✨ 概览

- 模式：Mode 1（定向扫描）、Mode 2（混合模糊扫描）
- 数据：支持地址批量地址输入、区块范围与实时监听扫描
- 能力：策略驱动、AI 多模型接入、Slither + LLM 组合验证
- 适用：学术实验、漏洞复现、链上批量扫描与监控

## ⚡ 快速开始

```bash
./install.sh
```

```bash
go build -o vespera src/main.go
./vespera --help
```

