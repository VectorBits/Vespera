package handler

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/VectorBits/Vespera/src/internal/ai"
	"github.com/VectorBits/Vespera/src/internal/ai/parser"
	"github.com/VectorBits/Vespera/src/internal/astparser"
	"github.com/VectorBits/Vespera/src/internal/config"
	"github.com/VectorBits/Vespera/src/internal/download"
	"github.com/VectorBits/Vespera/src/internal/logger"
	"github.com/VectorBits/Vespera/src/internal/solc"
	"github.com/VectorBits/Vespera/src/internal/static_analyzer"
	"github.com/VectorBits/Vespera/src/internal/ui"
	"github.com/VectorBits/Vespera/src/strategy/prompts"
)

// qhello RunMode2Fuzzy Mode2 混合扫描入口 (Slither + AI)
func RunMode2Fuzzy(ctx context.Context, cfg config.ScanConfiguration, targetChan <-chan string) error {
	ui.LogInfo("Mode 2 (Fuzzy Scan) Started")
	ui.LogInfo("Log file: logs/scan_....log (Check logs folder)")

	spinner := ui.StartSpinner("Initializing resources (DB, AI, Slither)...")
	resources, err := initMode2Resources(ctx, cfg)
	spinner <- true
	if err != nil {
		ui.LogError("Resource initialization failed: %v", err)
		return err
	}
	defer resources.Close()
	ui.LogSuccess("Resources initialized")

	downloader, err := download.NewDownloader(resources.DB, cfg.Chain, cfg.Proxy)
	if err != nil {
		return fmt.Errorf("failed to create downloader: %w", err)
	}
	downloader.SetConcurrency(cfg.Concurrency)
	defer downloader.Close()

	//helloq Worker Pool 并发扫描循环
	results := make([]*ScanResult, 0)
	stats := &scanStats{}
	var resultsMu sync.Mutex
	var reportOnce sync.Once

	writeReport := func() error {
		resultsMu.Lock()
		snapshot := append([]*ScanResult(nil), results...)
		resultsMu.Unlock()
		if len(snapshot) == 0 {
			return nil
		}
		return generateReport(snapshot, cfg)
	}

	go func() {
		<-ctx.Done()
		reportOnce.Do(func() {
			if err := writeReport(); err != nil {
				ui.LogError("Failed to generate partial report: %v", err)
			} else {
				ui.LogSuccess("Partial report generated (interrupted)")
			}
		})
	}()

	strategyName := cfg.Strategy
	if strategyName == "all" || strategyName == "" {
		strategyName = "default"
	}
	promptTemplate, err := prompts.LoadTemplate(cfg.Mode, strategyName)
	if err != nil {
		return fmt.Errorf("failed to load prompt template: %w", err)
	}

	concurrency := cfg.Concurrency
	if concurrency <= 0 {
		concurrency = 1
	}
	semaphore := make(chan struct{}, concurrency)
	var wg sync.WaitGroup

	ui.LogInfo("Starting concurrent scan (Workers: %d)...", concurrency)
	startTime := time.Now()

	idx := 0
	for {
		select {
		case <-ctx.Done():
			reportOnce.Do(func() {
				_ = writeReport()
			})
			return ctx.Err()
		case address, ok := <-targetChan:
			if !ok {
				wg.Wait()
				ui.PrintStats(idx, stats.successCount, stats.failCount, stats.vulnCount, time.Since(startTime))

				var reportErr error
				reportOnce.Do(func() {
					reportErr = writeReport()
				})
				if reportErr != nil {
					ui.LogError("Report generation failed: %v", reportErr)
					return fmt.Errorf("report generation failed: %w", reportErr)
				}
				if len(results) > 0 {
					ui.LogSuccess("Report generated successfully")
				}
				return nil
			}

			idx++
			wg.Add(1)
			semaphore <- struct{}{}

			go func(i int, addr string) {
				defer wg.Done()
				defer func() { <-semaphore }()

				defer func() {
					if r := recover(); r != nil {
						logger.Error("[PANIC] Contract %s: %v", addr, r)
						resultsMu.Lock()
						stats.failCount++
						resultsMu.Unlock()
					}
				}()

				ui.UpdateStatus("Scanning %s...", addr)
				logger.Info("========== [Task %d] Start: %s ==========", i, addr)

				result, err := processContractMode2(ctx, addr, cfg, resources, downloader, promptTemplate)

				if err != nil {
					logger.Warn("[Task %d] Failed: %v", i, err)
					resultsMu.Lock()
					stats.failCount++
					resultsMu.Unlock()
					return
				}
				if result == nil {
					logger.Info("[Task %d] Skipped", i)
					resultsMu.Lock()
					stats.failCount++
					resultsMu.Unlock()
					return
				}

				vulnCount := len(result.AnalysisResult.Vulnerabilities)
				resultsMu.Lock()
				results = append(results, result)
				stats.successCount++
				if vulnCount > 0 {
					stats.vulnCount += vulnCount
				}
				resultsMu.Unlock()

				if vulnCount > 0 {
					ui.LogVuln(addr, -1, vulnCount)
				}

			}(idx, address)
		}
	}
}

type mode2Resources struct {
	DB             *sql.DB
	AI             *ai.Manager
	StaticAnalyzer static_analyzer.Analyzer
	slitherCache   sync.Map
}

type slitherCacheEntry struct {
	result  *static_analyzer.AnalysisResult
	expires time.Time
}

const slitherCacheTTL = 5 * time.Minute

func (r *mode2Resources) Close() {
	if r.DB != nil {
		r.DB.Close()
	}
	if r.AI != nil {
		r.AI.Close()
	}
	if r.StaticAnalyzer != nil {
		r.StaticAnalyzer.Close()
	}
}

type scanStats struct {
	successCount int
	failCount    int
	vulnCount    int
}

func initMode2Resources(ctx context.Context, cfg config.ScanConfiguration) (*mode2Resources, error) {
	db, err := config.InitDB(ctx)
	if err != nil {
		return nil, fmt.Errorf("db init failed: %w", err)
	}

	logger.Info("Initializing Slither...")
	analyzerCfg := static_analyzer.DefaultConfig()
	sa, err := static_analyzer.NewAnalyzer(analyzerCfg)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("static analyzer init failed: %w", err)
	}

	logger.Info("Initializing AI Manager...")
	aiManager, err := ai.NewManager(ai.ManagerConfig{
		Provider:       cfg.AIProvider,
		Timeout:        cfg.Timeout,
		RequestsPerMin: 120,
		Strategy:       "mode2_fuzzy",
		APIKey:         cfg.APIKey,
		BaseURL:        cfg.BaseURL,
		Model:          cfg.Model,
		Proxy:          cfg.Proxy,
		Verbose:        cfg.Verbose,
	})
	if err != nil {
		db.Close()
		sa.Close()
		return nil, fmt.Errorf("ai manager init failed: %w", err)
	}

	if err := aiManager.TestConnection(ctx); err != nil {
		db.Close()
		sa.Close()
		aiManager.Close()
		return nil, fmt.Errorf("ai connection test failed: %w", err)
	}

	return &mode2Resources{
		DB:             db,
		AI:             aiManager,
		StaticAnalyzer: sa,
	}, nil
}

func resolveTargets(cfg config.ScanConfiguration, db *sql.DB) ([]string, error) {
	switch strings.ToLower(cfg.TargetSource) {
	case "db":
		return getAddressesFromDB(db, cfg.Chain, cfg.BlockRange)
	case "file", "filepath":
		return getAddressesFromFile(cfg.TargetFile)
	case "contract", "address", "single":
		if strings.TrimSpace(cfg.TargetAddress) == "" {
			return nil, fmt.Errorf("missing target address")
		}
		return []string{strings.TrimSpace(cfg.TargetAddress)}, nil
	default:
		return nil, fmt.Errorf("unsupported target source: %s", cfg.TargetSource)
	}
}

// qhello processContractMode2 单合约处理流程
func processContractMode2(ctx context.Context, address string, cfg config.ScanConfiguration, res *mode2Resources, downloader *download.Downloader, promptTemplate string) (*ScanResult, error) {
	contractCode, effectiveAddr, isProxy, err := getOrDownloadContract(ctx, res.DB, downloader, address)
	if err != nil {
		return nil, fmt.Errorf("get contract failed: %w", err)
	}

	cleanCode := normalizeContractCodeForAnalysis(contractCode)
	if isOnlyBytecode(cleanCode) {
		logger.Info("Contract %s is bytecode only, skipping", address)
		return nil, nil
	}

	//helloq Slither 静态分析
	cacheAddr := effectiveAddr
	if strings.TrimSpace(cacheAddr) == "" {
		cacheAddr = address
	}
	cacheKey := slitherCacheKey(cleanCode, cacheAddr)
	var staticResult *static_analyzer.AnalysisResult
	if cached, ok := res.slitherCache.Load(cacheKey); ok {
		entry := cached.(slitherCacheEntry)
		if time.Now().Before(entry.expires) && entry.result != nil {
			staticResult = entry.result
		} else {
			res.slitherCache.Delete(cacheKey)
		}
	}
	if staticResult == nil {
		staticResult, err = runSlitherAnalysis(ctx, res.StaticAnalyzer, cleanCode, effectiveAddr)
		if err != nil {
			logger.Warn("Slither analysis failed for %s: %v", address, err)
			return nil, nil
		}
		res.slitherCache.Store(cacheKey, slitherCacheEntry{
			result:  staticResult,
			expires: time.Now().Add(slitherCacheTTL),
		})
	}

	//helloq AI 验证 Slither 结果
	verifiedVulns, stats, rawResponses, err := verifyDetectors(ctx, res.AI, staticResult, cleanCode, effectiveAddr, promptTemplate)
	if err != nil {
		return nil, err
	}

	summary := fmt.Sprintf("Slither: %d | Verified: %d | FP: %d",
		len(staticResult.Detectors), stats.Verified, stats.FalsePositives)

	rawResp := fmt.Sprintf("Slither Results: %d\nAI Verified: %d",
		len(staticResult.Detectors), stats.Verified)
	if strings.TrimSpace(rawResponses) != "" {
		rawResp = rawResp + "\n\n" + rawResponses
	}

	scanResult := &ScanResult{
		Address:         address,
		ResolvedAddress: effectiveAddr,
		IsProxy:         isProxy,
		AnalysisResult: &parser.AnalysisResult{
			Vulnerabilities: verifiedVulns,
			Summary:         summary,
			RawResponse:     rawResp,
		},
		Timestamp: time.Now(),
		Mode:      cfg.Mode,
		Strategy:  "slither_scan",
	}

	return scanResult, nil
}

func runSlitherAnalysis(ctx context.Context, analyzer static_analyzer.Analyzer, code, address string) (*static_analyzer.AnalysisResult, error) {
	solcVersion := extractSolidityVersion(code)
	if solcVersion == "" {
		solcVersion = "0.8.0"
		logger.Warn("No pragma found, defaulting to 0.8.0")
	} else {
		solcVersion = normalizeSolidityVersion(solcVersion)
		logger.Debug("Detected Solidity version: %s", solcVersion)
	}

	logger.Info("Running Slither analysis on %s...", address)

	result, err := analyzer.AnalyzeContract(ctx, code, &static_analyzer.AnalysisConfig{
		ContractName: "Contract",
		SolcVersion:  solcVersion,
		Address:      address,
		Optimization: false,
		ViaIR:        false,
	})

	if err != nil {
		errMsg := err.Error()
		if strings.Contains(errMsg, "constructor()") || strings.Contains(errMsg, "emit") || strings.Contains(errMsg, "Invalid compilation") {
			return nil, fmt.Errorf("slither compilation failed: %v", err)
		}
		return nil, fmt.Errorf("slither analysis failed: %v", err)
	}

	logger.Info("Slither finished. Found %d issues.", len(result.Detectors))
	return result, nil
}

func slitherCacheKey(code, address string) string {
	sum := sha256.Sum256([]byte(code))
	return address + ":" + hex.EncodeToString(sum[:])
}

type VerificationStats struct {
	Verified       int
	FalsePositives int
}

func buildMode2CallGraphContext(code string) (bool, string, string, string) {
	parsedSource, err := astparser.ParseSource(code)
	if err != nil {
		return false, "", "", ""
	}

	cg := parsedSource.BuildCallGraph()
	if cg == nil {
		return false, "", "", ""
	}

	var infoBuilder strings.Builder
	entryPoints := cg.GetPublicEntryPoints()
	infoBuilder.WriteString(fmt.Sprintf("// Call Graph Summary:\n"))
	infoBuilder.WriteString(fmt.Sprintf("// - Total Functions: %d\n", len(cg.Functions)))
	infoBuilder.WriteString(fmt.Sprintf("// - Public Entry Points: %d\n", len(entryPoints)))
	internalFuncs := len(cg.Functions) - len(entryPoints)
	if internalFuncs < 0 {
		internalFuncs = 0
	}
	infoBuilder.WriteString(fmt.Sprintf("// - Internal Functions: %d\n", internalFuncs))

	enrichedContext := cg.GenerateCallGraphTree()

	var calleesBuilder strings.Builder
	const maxCalleesChars = 12000
	relatedFuncs := cg.GetAllRelatedFunctions(3)
	for _, node := range relatedFuncs {
		if node == nil {
			continue
		}
		if calleesBuilder.Len() >= maxCalleesChars {
			break
		}
		ref := cg.FunctionRefs[node.ID]
		if ref != nil {
			calleesBuilder.WriteString(fmt.Sprintf("// Related Function: %s.%s\n", ref.ContractName, ref.FunctionName))
		} else {
			calleesBuilder.WriteString(fmt.Sprintf("// Related Function: %s\n", node.Name))
		}
		calleesBuilder.WriteString(parsedSource.GetSourceRange(node.Src) + "\n\n")
	}

	return true, infoBuilder.String(), calleesBuilder.String(), enrichedContext
}

func verifyDetectors(ctx context.Context, aiManager *ai.Manager, staticResult *static_analyzer.AnalysisResult, code, address, template string) ([]parser.Vulnerability, VerificationStats, string, error) {
	var verifiedVulns []parser.Vulnerability
	stats := VerificationStats{}
	rawBlocks := make([]string, 0, len(staticResult.Detectors))

	if len(staticResult.Detectors) == 0 {
		logger.Info("No static issues found, skipping AI verification")
		return verifiedVulns, stats, "", nil
	}

	prettyCode := normalizeContractCodeForPrompt(code)
	enableCallGraph, callGraphInfo, calleesCode, enrichedContext := buildMode2CallGraphContext(code)

	for i, detector := range staticResult.Detectors {
		logger.Debug("Verifying issue %d/%d: %s", i+1, len(staticResult.Detectors), detector.Check)

		prompt := prompts.BuildPrompt(template, map[string]interface{}{
			"ContractAddress":     address,
			"ContractCode":        prettyCode,
			"EnableCallGraph":     enableCallGraph,
			"CallGraphInfo":       callGraphInfo,
			"CalleesCode":         calleesCode,
			"EnrichedContext":     enrichedContext,
			"DetectorCheck":       detector.Check,
			"DetectorImpact":      detector.Impact,
			"DetectorConfidence":  detector.Confidence,
			"DetectorDescription": detector.Description,
			"LineNumbers":         detector.LineNumbers,
		})

		logger.Debug("Prompt:\n%s", prompt)

		aiResult, err := aiManager.AnalyzeContractWithStrategy(ctx, code, prompt, "mode2_fuzzy")
		if err != nil {
			logger.Error("AI verification failed: %v", err)
			continue
		}

		logger.Debug("AI Response:\n%s", aiResult.RawResponse)
		rawBlocks = append(rawBlocks, fmt.Sprintf("Detector: %s\nResponse:\n%s", detector.Check, aiResult.RawResponse))

		verificationResult, parseErr := aiManager.GetParser().ParseVerificationResult(aiResult.RawResponse)

		isReal := false
		reason := "Parse failed"
		severity := "Unknown"

		if parseErr == nil {
			isReal = verificationResult.IsVulnerability
			reason = verificationResult.Reason
			severity = verificationResult.Severity
		} else {
			logger.Warn("Failed to parse AI response: %v", parseErr)
			rawBlocks[len(rawBlocks)-1] = fmt.Sprintf("Detector: %s\nParseError: %v\nResponse:\n%s", detector.Check, parseErr, aiResult.RawResponse)
			continue
		}

		logger.Info("AI Decision: Real=%v, Severity=%s, Reason=%s", isReal, severity, reason)

		if isReal {
			stats.Verified++
			vuln := parser.Vulnerability{
				Type:        detector.Check,
				Severity:    severity,
				Description: fmt.Sprintf("Slither: %s\nAI: Confirmed\nReason: %s", detector.Description, reason),
				Location:    address,
				LineNumbers: detector.LineNumbers,
				References:  []string{fmt.Sprintf("Slither Detector: %s", detector.Check)},
			}
			verifiedVulns = append(verifiedVulns, vuln)
		} else {
			stats.FalsePositives++
		}
	}

	return verifiedVulns, stats, strings.Join(rawBlocks, "\n\n-----\n\n"), nil
}

func normalizeContractCodeForPrompt(code string) string {
	lines := strings.Split(code, "\n")
	var sb strings.Builder
	for i, line := range lines {
		sb.WriteString(fmt.Sprintf("%d: %s\n", i+1, line))
	}
	return sb.String()
}

func normalizeContractCodeForAnalysis(code string) string {
	cleaned, _ := solc.DetachMetadata(code)
	cleaned = strings.TrimSpace(cleaned)
	if strings.HasPrefix(cleaned, "{{") && strings.HasSuffix(cleaned, "}}") && len(cleaned) >= 4 {
		cleaned = strings.TrimSpace(cleaned[1 : len(cleaned)-1])
	}
	if solc.IsJSONSource(cleaned) {
		if flattened, err := solc.FlattenJSONSource(cleaned); err == nil && strings.TrimSpace(flattened) != "" {
			cleaned = flattened
		}
	}
	return cleaned
}
