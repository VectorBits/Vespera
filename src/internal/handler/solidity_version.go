package handler

import (
	"regexp"
	"strconv"
	"strings"
)

func extractSolidityVersion(code string) string {
	// 匹配所有 pragma solidity 声明（可能有多个）
	re := regexp.MustCompile(`pragma\s+solidity\s+([^;]+);`)
	allMatches := re.FindAllStringSubmatch(code, -1)
	if len(allMatches) == 0 {
		return "" // 未找到 pragma 声明
	}

	// 收集所有版本号
	var versions []string
	versionRe := regexp.MustCompile(`(\d+\.\d+)(?:\.(\d+))?`)

	for _, matches := range allMatches {
		versionStr := strings.TrimSpace(matches[1])
		if picked := pickVersionFromConstraint(versionStr); picked != "" {
			versions = append(versions, picked)
			continue
		}
		versionMatches := versionRe.FindAllStringSubmatch(versionStr, -1)

		for _, vm := range versionMatches {
			if vm[2] != "" {
				// 完整版本号 x.y.z
				versions = append(versions, vm[1]+"."+vm[2])
			} else {
				// 只有 x.y，补全为 x.y.0
				versions = append(versions, vm[1]+".0")
			}
		}
	}

	if len(versions) == 0 {
		return ""
	}

	// 返回最高的版本号（通常是最兼容的）
	maxVersion := versions[0]
	for _, v := range versions[1:] {
		if compareVersions(v, maxVersion) > 0 {
			maxVersion = v
		}
	}

	return maxVersion
}

func pickVersionFromConstraint(versionStr string) string {
	upperRe := regexp.MustCompile(`(<=|<)\s*(\d+)\.(\d+)(?:\.(\d+))?`)
	m := upperRe.FindStringSubmatch(versionStr)
	if m == nil {
		return ""
	}
	op := m[1]
	major, _ := strconv.Atoi(m[2])
	minor, _ := strconv.Atoi(m[3])
	if op == "<=" {
		if m[4] != "" {
			return m[2] + "." + m[3] + "." + m[4]
		}
		return m[2] + "." + m[3] + ".0"
	}
	if major == 0 {
		switch minor {
		case 8:
			return "0.7.6"
		case 7:
			return "0.6.12"
		case 6:
			return "0.5.17"
		case 5:
			return "0.4.26"
		}
	}
	return ""
}

func compareVersions(v1, v2 string) int {
	parts1 := strings.Split(v1, ".")
	parts2 := strings.Split(v2, ".")

	for i := 0; i < 3; i++ {
		var p1, p2 int
		if i < len(parts1) {
			p1, _ = strconv.Atoi(parts1[i])
		}
		if i < len(parts2) {
			p2, _ = strconv.Atoi(parts2[i])
		}

		if p1 > p2 {
			return 1
		} else if p1 < p2 {
			return -1
		}
	}

	return 0
}

func normalizeSolidityVersion(version string) string {
	if version == "" {
		return "0.8.0" // 默认版本
	}

	// 确保版本号格式正确（x.y.z）
	parts := strings.Split(version, ".")
	if len(parts) < 2 {
		return "0.8.0" // 默认版本
	}

	// 如果只有两部分（如 0.4），补全为 0.4.0
	if len(parts) == 2 {
		return version + ".0"
	}

	return version
}
