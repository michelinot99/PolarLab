// Esegue le primitive numeriche su segnali di prova fissi e ne stampa i risultati in JSON:
// scripts/compare_swift.py li ricalcola con numpy/scipy e confronta.

import Foundation

public func dspReport() -> String {
    let n = 500
    let x = (0..<n).map { i -> Double in
        let d = Double(i)
        return sin(0.05 * d) + 0.5 * sin(0.31 * d) + 0.2 * cos(1.7 * d) + 0.001 * d * d / 100
    }
    var out: [String: Any] = [:]
    let filters: [(String, [Section])] = [
        ("bp_ecg", butter(2, [0.5, 40], .bandpass, fs: 130)),
        ("bp_qrs", butter(2, [5, 15], .bandpass, fs: 130)),
        ("lp_grav", butter(2, [0.3], .lowpass, fs: 100)),
        ("bp_cad", butter(2, [1, 6], .bandpass, fs: 100)),
        ("lp_resp", butter(2, [1.0], .lowpass, fs: 10)),
        ("hp_osc", butter(2, [0.5 / 0.36 * 0.5], .highpass, fs: 100)),
        ("notch", notchSOS(fs: 130)),
    ]
    for (name, sos) in filters { out["sos_" + name] = sos }
    out["sosfilt_bp_ecg"] = SosFilter(butter(2, [0.5, 40], .bandpass, fs: 130)).process(x)
    out["sosfiltfilt_bp_cad"] = sosfiltfilt(butter(2, [1, 6], .bandpass, fs: 100), x)
    out["sosfiltfilt_lp_resp"] = sosfiltfilt(butter(2, [1.0], .lowpass, fs: 10), x)
    out["detrend"] = detrendLinear(x)
    let w1 = welch(x, fs: 4, nperseg: 128, noverlap: 64, detrendConstant: true)
    out["welch_const"] = w1.p
    let w2 = welch(x, fs: 4, nperseg: 256, noverlap: 128, detrendConstant: false)
    out["welch_none"] = w2.p
    let w3 = welch(Array(x.prefix(301)), fs: 10, nperseg: 301, noverlap: 150, detrendConstant: true)
    out["welch_odd"] = w3.p

    var t = [0.0]
    for k in 1..<80 { t.append(t[k - 1] + 0.7 + 0.15 * sin(Double(k))) }
    let y = t.map { 800 + 40 * sin(1.3 * $0) + 10 * cos(0.2 * $0) }
    let tu = arange(t[0], t[t.count - 1], 0.25)
    out["spline_t"] = t
    out["spline"] = cubicSpline(t, y)(tu)
    out["pchip"] = pchip(t, y)(tu)
    out["percentiles"] = [percentile(x, 25), percentile(x, 75), percentile(x, 99.5), percentile(x, 0.5), median(x)]
    out["dfa"] = [dfaAlpha(y), dfaAlpha(Array(x.prefix(200)))]
    out["peaks"] = findPeaks([0, 1, 1, 0, 2, 3, 3, 3, 1, 4, 4, 5, 2, 2, 6]).map(Double.init)
    let cov: [[Double]] = [[4, 1, 0.5], [1, 3, 0.2], [0.5, 0.2, 1]]
    let ax = principalAxis(cov)
    out["axis"] = [ax.x, ax.y, ax.z]
    if let fd = frequencyDomain(t: t.map { $0 * 1.5 }, nn: y) { out["freq"] = [fd.lf, fd.hf, fd.lfHf, fd.respPeakHz] }

    let data = try! JSONSerialization.data(withJSONObject: out.mapValues { v -> Any in
        if let a = v as? [Double] { return a.map { $0.isFinite ? $0 : -999 } }
        return v
    }, options: [.sortedKeys])
    return String(data: data, encoding: .utf8)!
}
