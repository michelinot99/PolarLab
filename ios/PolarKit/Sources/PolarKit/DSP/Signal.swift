// Primitive numeriche che in Python vengono da numpy/scipy, riscritte con gli stessi
// algoritmi (stesse convenzioni di scipy) per avere risultati confrontabili 1:1.

import Foundation

// MARK: - Statistiche (numpy)

@inlinable func mean<C: Collection>(_ x: C) -> Double where C.Element == Double {
    x.isEmpty ? .nan : x.reduce(0, +) / Double(x.count)
}

func median<C: Collection>(_ x: C) -> Double where C.Element == Double {
    if x.isEmpty { return .nan }
    let s = x.sorted()
    let n = s.count
    return n % 2 == 1 ? s[n / 2] : 0.5 * (s[n / 2 - 1] + s[n / 2])
}

/// np.percentile con interpolazione lineare (default di numpy).
func percentile(_ x: [Double], _ q: Double) -> Double {
    if x.isEmpty { return .nan }
    return percentileSorted(x.sorted(), q)
}

func percentileSorted(_ s: [Double], _ q: Double) -> Double {
    let pos = q / 100.0 * Double(s.count - 1)
    let lo = Int(pos.rounded(.down))
    let hi = min(lo + 1, s.count - 1)
    let f = pos - Double(lo)
    return s[lo] + (s[hi] - s[lo]) * f
}

func std(_ x: [Double], ddof: Int = 0) -> Double {
    guard x.count > ddof else { return .nan }
    let m = mean(x)
    let ss = x.reduce(0) { $0 + ($1 - m) * ($1 - m) }
    return (ss / Double(x.count - ddof)).squareRoot()
}

func diff(_ x: [Double]) -> [Double] {
    x.count < 2 ? [] : (1..<x.count).map { x[$0] - x[$0 - 1] }
}

func cumsum(_ x: [Double]) -> [Double] {
    var acc = 0.0
    return x.map { acc += $0; return acc }
}

/// np.interp: interpolazione lineare con valori costanti fuori dall'intervallo.
func interp(_ xs: [Double], _ xp: [Double], _ fp: [Double]) -> [Double] {
    var out = [Double](repeating: 0, count: xs.count)
    var j = 0
    let n = xp.count
    for (i, x) in xs.enumerated() {
        if x <= xp[0] { out[i] = fp[0]; continue }
        if x >= xp[n - 1] { out[i] = fp[n - 1]; continue }
        if j > 0, x < xp[j] { j = 0 }
        while j < n - 2, xp[j + 1] <= x { j += 1 }
        let f = (x - xp[j]) / (xp[j + 1] - xp[j])
        out[i] = fp[j] + f * (fp[j + 1] - fp[j])
    }
    return out
}

func corrcoef(_ a: [Double], _ b: [Double]) -> Double {
    let ma = mean(a), mb = mean(b)
    var sab = 0.0, saa = 0.0, sbb = 0.0
    for i in 0..<a.count {
        let da = a[i] - ma, db = b[i] - mb
        sab += da * db
        saa += da * da
        sbb += db * db
    }
    return sab / (saa * sbb).squareRoot()
}

/// Pendenza della retta ai minimi quadrati (np.polyfit(x, y, 1)[0]).
func polyfitSlope(_ x: [Double], _ y: [Double]) -> Double {
    let mx = mean(x), my = mean(y)
    var sxy = 0.0, sxx = 0.0
    for i in 0..<x.count {
        sxy += (x[i] - mx) * (y[i] - my)
        sxx += (x[i] - mx) * (x[i] - mx)
    }
    return sxy / sxx
}

/// scipy.signal.detrend(type="linear"): toglie la retta ai minimi quadrati.
func detrendLinear(_ y: [Double]) -> [Double] {
    let n = y.count
    if n < 2 { return y.map { $0 - mean(y) } }
    let xm = Double(n - 1) / 2.0
    let ym = mean(y)
    var sxy = 0.0, sxx = 0.0
    for i in 0..<n {
        let dx = Double(i) - xm
        sxy += dx * (y[i] - ym)
        sxx += dx * dx
    }
    let b = sxy / sxx
    return (0..<n).map { y[$0] - ym - b * (Double($0) - xm) }
}

func trapezoid(_ y: [Double], _ x: [Double]) -> Double {
    var s = 0.0
    for i in 1..<max(1, y.count) { s += 0.5 * (y[i] + y[i - 1]) * (x[i] - x[i - 1]) }
    return s
}

// MARK: - Numeri complessi minimi (progetto dei filtri)

struct Complex {
    var re: Double, im: Double
    init(_ re: Double, _ im: Double = 0) { self.re = re; self.im = im }
    static func + (a: Complex, b: Complex) -> Complex { Complex(a.re + b.re, a.im + b.im) }
    static func - (a: Complex, b: Complex) -> Complex { Complex(a.re - b.re, a.im - b.im) }
    static func * (a: Complex, b: Complex) -> Complex { Complex(a.re * b.re - a.im * b.im, a.re * b.im + a.im * b.re) }
    static func * (a: Double, b: Complex) -> Complex { Complex(a * b.re, a * b.im) }
    static func / (a: Complex, b: Complex) -> Complex {
        let d = b.re * b.re + b.im * b.im
        return Complex((a.re * b.re + a.im * b.im) / d, (a.im * b.re - a.re * b.im) / d)
    }
    var abs: Double { hypot(re, im) }
    var conj: Complex { Complex(re, -im) }
    var isReal: Bool { im == 0 }
    func sqrt() -> Complex {
        let r = abs
        let a = ((r + re) / 2).squareRoot()
        let b = ((r - re) / 2).squareRoot()
        return Complex(a, im < 0 ? -b : b)
    }
}

// MARK: - Progetto filtri (scipy.signal.butter / iirnotch, uscita SOS)

/// Sezione del secondo ordine [b0, b1, b2, a0=1, a1, a2].
public typealias Section = [Double]

public enum FilterType { case lowpass, highpass, bandpass }

/// scipy.signal.butter(order, wn, btype, fs=fs, output="sos").
public func butter(_ order: Int, _ wn: [Double], _ btype: FilterType, fs: Double) -> [Section] {
    // Prototipo analogico (buttap)
    var p: [Complex] = stride(from: -order + 1, to: order, by: 2).map { m in
        let a = Double.pi * Double(m) / Double(2 * order)
        return Complex(-cos(a), -sin(a))
    }
    var z: [Complex] = []
    var k = 1.0
    // Pre-distorsione (scipy normalizza a fs = 2)
    let fsN = 2.0
    let warped = wn.map { 2 * fsN * tan(Double.pi * (2 * $0 / fs) / fsN) }
    switch btype {
    case .lowpass:
        let wo = warped[0]
        let degree = p.count - z.count
        z = z.map { wo * $0 }
        p = p.map { wo * $0 }
        k *= pow(wo, Double(degree))
    case .highpass:
        let wo = warped[0]
        let degree = p.count - z.count
        let num = z.reduce(Complex(1)) { $0 * Complex(-$1.re, -$1.im) }
        let den = p.reduce(Complex(1)) { $0 * Complex(-$1.re, -$1.im) }
        k *= (num / den).re
        z = z.map { Complex(wo) / $0 } + Array(repeating: Complex(0), count: degree)
        p = p.map { Complex(wo) / $0 }
    case .bandpass:
        let bw = warped[1] - warped[0]
        let wo = (warped[0] * warped[1]).squareRoot()
        let degree = p.count - z.count
        func bp(_ r: [Complex]) -> [Complex] {
            let lp = r.map { (bw / 2) * $0 }
            let disc = lp.map { ($0 * $0 - Complex(wo * wo)).sqrt() }
            return zip(lp, disc).map { $0 + $1 } + zip(lp, disc).map { $0 - $1 }
        }
        z = bp(z) + Array(repeating: Complex(0), count: degree)
        p = bp(p)
        k *= pow(bw, Double(degree))
    }
    // Trasformazione bilineare
    let fs2 = 2 * fsN
    let degree = p.count - z.count
    let zz = z.map { (Complex(fs2) + $0) / (Complex(fs2) - $0) } + Array(repeating: Complex(-1), count: degree)
    let pz = p.map { (Complex(fs2) + $0) / (Complex(fs2) - $0) }
    let num = z.reduce(Complex(1)) { $0 * (Complex(fs2) - $1) }
    let den = p.reduce(Complex(1)) { $0 * (Complex(fs2) - $1) }
    k *= (num / den).re
    return zpk2sos(zz, pz, k)
}

/// scipy.signal.zpk2sos con accoppiamento "nearest", per filtri con poli in coppie coniugate.
func zpk2sos(_ z: [Complex], _ p: [Complex], _ k: Double) -> [Section] {
    let tol = 1e-9
    func upper(_ r: [Complex]) -> [Complex] {
        // Una sola radice per coppia coniugata (parte immaginaria ≥ 0), poi le reali
        let cplx = r.filter { $0.im > tol }.sorted { ($0.re, $0.im) < ($1.re, $1.im) }
        let real = r.filter { Swift.abs($0.im) <= tol }.map { Complex($0.re) }.sorted { $0.re < $1.re }
        return cplx + real
    }
    var zs = upper(z), ps = upper(p)
    let nSections = (max(p.count, z.count) + 1) / 2
    var sections: [(z: [Complex], p: [Complex])] = []
    for _ in 0..<nSections {
        let i1 = ps.indices.min { Swift.abs(1 - ps[$0].abs) < Swift.abs(1 - ps[$1].abs) }!
        let p1 = ps.remove(at: i1)
        var pp: [Complex]
        if p1.isReal {
            if let i2 = ps.indices.filter({ ps[$0].isReal }).min(by: { Swift.abs(1 - ps[$0].abs) < Swift.abs(1 - ps[$1].abs) }) {
                pp = [p1, ps.remove(at: i2)]
            } else {
                pp = [p1]
            }
        } else {
            pp = [p1, p1.conj]
        }
        var zz: [Complex] = []
        if !zs.isEmpty {
            let i1 = zs.indices.min { (zs[$0] - p1).abs < (zs[$1] - p1).abs }!
            let z1 = zs.remove(at: i1)
            zz = [z1]
            if !z1.isReal {
                zz.append(z1.conj)
            } else if let i2 = zs.indices.filter({ zs[$0].isReal }).min(by: { (zs[$0] - p1).abs < (zs[$1] - p1).abs }) {
                zz.append(zs.remove(at: i2))
            }
        }
        sections.append((zz, pp))
    }
    func poly(_ r: [Complex]) -> [Double] {
        var c = [Complex(1)]
        for root in r {
            var next = [Complex](repeating: Complex(0), count: c.count + 1)
            for i in 0..<c.count {
                next[i] = next[i] + c[i]
                next[i + 1] = next[i + 1] - c[i] * root
            }
            c = next
        }
        var out = c.map(\.re)
        while out.count < 3 { out.append(0) }
        return out
    }
    var sos: [Section] = sections.reversed().map { poly($0.z) + poly($0.p) }
    for j in 0..<3 { sos[0][j] *= k }
    return sos
}

/// scipy.signal.iirnotch + tf2sos (una sezione).
public func notchSOS(fs: Double, f0: Double = 50.0, q: Double = 30.0) -> [Section] {
    let w0 = 2 * f0 / fs * Double.pi
    let bw = (2 * f0 / fs) / q * Double.pi
    let beta = tan(bw / 2.0)
    let gain = 1.0 / (1.0 + beta)
    return [[gain, -2 * gain * cos(w0), gain, 1.0, -2 * gain * cos(w0), 2 * gain - 1.0]]
}

// MARK: - Filtraggio (scipy.signal.sosfilt / sosfilt_zi / sosfiltfilt / lfilter)

/// Stato a regime per ingresso costante unitario (sosfilt_zi).
func sosfiltZi(_ sos: [Section]) -> [[Double]] {
    var scale = 1.0
    return sos.map { s in
        let (b0, b1, b2, a1, a2) = (s[0] / s[3], s[1] / s[3], s[2] / s[3], s[4] / s[3], s[5] / s[3])
        // (I − Aᵀ) zi = B, con A matrice compagna di a
        let B0 = b1 - a1 * b0, B1 = b2 - a2 * b0
        let det = (1 + a1) * 1 - (-1) * a2
        let z0 = (B0 * 1 - (-1) * B1) / det
        let z1 = ((1 + a1) * B1 - a2 * B0) / det
        defer { scale *= (s[0] + s[1] + s[2]) / (s[3] + s[4] + s[5]) }
        return [z0 * scale, z1 * scale]
    }
}

/// Forma diretta II trasposta, sezione per sezione; aggiorna `zi` in place.
func sosfilt(_ sos: [Section], _ x: [Double], _ zi: inout [[Double]]) -> [Double] {
    var y = x
    for (k, s) in sos.enumerated() {
        let b0 = s[0], b1 = s[1], b2 = s[2], a1 = s[4], a2 = s[5]
        var z0 = zi[k][0], z1 = zi[k][1]
        for i in 0..<y.count {
            let xi = y[i]
            let yi = b0 * xi + z0
            z0 = b1 * xi - a1 * yi + z1
            z1 = b2 * xi - a2 * yi
            y[i] = yi
        }
        zi[k] = [z0, z1]
    }
    return y
}

/// scipy.signal.sosfiltfilt con padtype="odd" e padlen di default.
public func sosfiltfilt(_ sos: [Section], _ x: [Double]) -> [Double] {
    let zerosB = sos.filter { $0[2] == 0 }.count, zerosA = sos.filter { $0[5] == 0 }.count
    let padlen = min(3 * (2 * sos.count + 1 - min(zerosB, zerosA)), x.count - 1)
    let n = x.count
    guard n > 1 else { return x }
    var ext = [Double]()
    ext.reserveCapacity(n + 2 * padlen)
    for i in stride(from: padlen, through: 1, by: -1) { ext.append(2 * x[0] - x[i]) }
    ext += x
    for i in 1...max(1, padlen) where padlen > 0 { ext.append(2 * x[n - 1] - x[n - 1 - i]) }
    let zi = sosfiltZi(sos)
    var z = zi.map { $0.map { $0 * ext[0] } }
    var y = sosfilt(sos, ext, &z)
    let y0 = y[y.count - 1]
    z = zi.map { $0.map { $0 * y0 } }
    y = sosfilt(sos, y.reversed(), &z)
    y.reverse()
    return Array(y[padlen..<(padlen + n)])
}

// MARK: - Spettro (scipy.signal.welch, scaling="density", finestra di Hann periodica)

func welch(_ x: [Double], fs: Double, nperseg: Int, noverlap: Int, detrendConstant: Bool) -> (f: [Double], p: [Double]) {
    let n = nperseg
    let step = n - noverlap
    let nseg = (x.count - n) / step + 1
    let win = (0..<n).map { 0.5 - 0.5 * cos(2 * Double.pi * Double($0) / Double(n)) }
    let scale = 1.0 / (fs * win.reduce(0) { $0 + $1 * $1 })
    let nf = n / 2 + 1
    var cosT = [Double](repeating: 0, count: n), sinT = cosT
    for i in 0..<n {
        cosT[i] = cos(2 * Double.pi * Double(i) / Double(n))
        sinT[i] = sin(2 * Double.pi * Double(i) / Double(n))
    }
    var psd = [Double](repeating: 0, count: nf)
    for s in 0..<nseg {
        var seg = Array(x[(s * step)..<(s * step + n)])
        if detrendConstant {
            let m = mean(seg)
            seg = seg.map { $0 - m }
        }
        for i in 0..<n { seg[i] *= win[i] }
        for k in 0..<nf {
            var re = 0.0, im = 0.0
            var idx = 0
            for i in 0..<n {
                re += seg[i] * cosT[idx]
                im -= seg[i] * sinT[idx]
                idx += k
                if idx >= n { idx -= n }
            }
            psd[k] += (re * re + im * im) * scale
        }
    }
    for k in 0..<nf {
        psd[k] /= Double(nseg)
        let isNyquist = n % 2 == 0 && k == nf - 1
        if k > 0 && !isNyquist { psd[k] *= 2 }
    }
    return ((0..<nf).map { Double($0) * fs / Double(n) }, psd)
}

// MARK: - Interpolazione (scipy.interpolate)

/// Polinomi a tratti in forma di Horner sugli intervalli [x[i], x[i+1]).
struct PiecewiseCubic {
    let x: [Double]
    let c: [(Double, Double, Double, Double)]

    /// Spline cubica di Hermite: valori `y` e derivate `d` ai nodi.
    init(x: [Double], y: [Double], d: [Double]) {
        self.x = x
        var c: [(Double, Double, Double, Double)] = []
        c.reserveCapacity(x.count - 1)
        for i in 0..<(x.count - 1) {
            let dx = x[i + 1] - x[i]
            let slope = (y[i + 1] - y[i]) / dx
            let t = (d[i] + d[i + 1] - 2 * slope) / dx
            c.append((t / dx, (slope - d[i]) / dx - t, d[i], y[i]))
        }
        self.c = c
    }

    func callAsFunction(_ xs: [Double]) -> [Double] {
        var j = 0
        let last = x.count - 2
        return xs.map { v in
            while j < last, x[j + 1] <= v { j += 1 }
            while j > 0, v < x[j] { j -= 1 }
            let h = v - x[j]
            let (c0, c1, c2, c3) = c[j]
            return ((c0 * h + c1) * h + c2) * h + c3
        }
    }
}

/// scipy.interpolate.CubicSpline(x, y) con condizioni "not-a-knot" (n > 3).
func cubicSpline(_ x: [Double], _ y: [Double]) -> PiecewiseCubic {
    let n = x.count
    let dx = diff(x)
    let slope = (0..<(n - 1)).map { (y[$0 + 1] - y[$0]) / dx[$0] }
    // Sistema tridiagonale nelle derivate ai nodi
    var lower = [Double](repeating: 0, count: n), diag = lower, upper = lower, b = lower
    for i in 1..<(n - 1) {
        lower[i] = dx[i]
        diag[i] = 2 * (dx[i - 1] + dx[i])
        upper[i] = dx[i - 1]
        b[i] = 3 * (dx[i] * slope[i - 1] + dx[i - 1] * slope[i])
    }
    var d = x[2] - x[0]
    diag[0] = dx[1]
    upper[0] = d
    b[0] = ((dx[0] + 2 * d) * dx[1] * slope[0] + dx[0] * dx[0] * slope[1]) / d
    d = x[n - 1] - x[n - 3]
    diag[n - 1] = dx[n - 3]
    lower[n - 1] = d
    b[n - 1] = (dx[n - 2] * dx[n - 2] * slope[n - 3] + (2 * d + dx[n - 2]) * dx[n - 3] * slope[n - 2]) / d
    // Algoritmo di Thomas
    var cp = [Double](repeating: 0, count: n), dp = cp
    cp[0] = upper[0] / diag[0]
    dp[0] = b[0] / diag[0]
    for i in 1..<n {
        let m = diag[i] - lower[i] * cp[i - 1]
        cp[i] = i < n - 1 ? upper[i] / m : 0
        dp[i] = (b[i] - lower[i] * dp[i - 1]) / m
    }
    var s = [Double](repeating: 0, count: n)
    s[n - 1] = dp[n - 1]
    for i in stride(from: n - 2, through: 0, by: -1) { s[i] = dp[i] - cp[i] * s[i + 1] }
    return PiecewiseCubic(x: x, y: y, d: s)
}

/// scipy.interpolate.PchipInterpolator (Fritsch–Butland, bordi a tre punti).
func pchip(_ x: [Double], _ y: [Double]) -> PiecewiseCubic {
    let n = x.count
    let h = diff(x)
    let m = (0..<(n - 1)).map { (y[$0 + 1] - y[$0]) / h[$0] }
    var d = [Double](repeating: 0, count: n)
    func sign(_ v: Double) -> Double { v > 0 ? 1 : v < 0 ? -1 : 0 }
    for k in 1..<(n - 1) {
        if sign(m[k]) != sign(m[k - 1]) || m[k] == 0 || m[k - 1] == 0 { continue }
        let w1 = 2 * h[k] + h[k - 1], w2 = h[k] + 2 * h[k - 1]
        d[k] = (w1 + w2) / (w1 / m[k - 1] + w2 / m[k])
    }
    func edge(_ h0: Double, _ h1: Double, _ m0: Double, _ m1: Double) -> Double {
        let e = ((2 * h0 + h1) * m0 - h0 * m1) / (h0 + h1)
        if sign(e) != sign(m0) { return 0 }
        if sign(m0) != sign(m1) && Swift.abs(e) > 3 * Swift.abs(m0) { return 3 * m0 }
        return e
    }
    d[0] = edge(h[0], h[1], m[0], m[1])
    d[n - 1] = edge(h[n - 2], h[n - 3], m[n - 2], m[n - 3])
    return PiecewiseCubic(x: x, y: y, d: d)
}

// MARK: - Varie

/// scipy.signal.find_peaks(x) senza parametri: massimi locali, al centro dei plateau.
func findPeaks(_ x: [Double]) -> [Int] {
    var out: [Int] = []
    var i = 1
    let iMax = x.count - 1
    while i < iMax {
        if x[i - 1] < x[i] {
            var ahead = i + 1
            while ahead < iMax, x[ahead] == x[i] { ahead += 1 }
            if x[ahead] < x[i] {
                out.append((i + ahead - 1) / 2)
                i = ahead
            }
        }
        i += 1
    }
    return out
}

/// Autovettore dell'autovalore maggiore di una matrice simmetrica 3×3 (Jacobi).
func principalAxis(_ m: [[Double]]) -> SIMD3<Double> {
    var a = m
    var v: [[Double]] = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    for _ in 0..<50 {
        var p = 0, q = 1
        var big = Swift.abs(a[0][1])
        if Swift.abs(a[0][2]) > big { (p, q, big) = (0, 2, Swift.abs(a[0][2])) }
        if Swift.abs(a[1][2]) > big { (p, q, big) = (1, 2, Swift.abs(a[1][2])) }
        if big < 1e-12 * (Swift.abs(a[0][0]) + Swift.abs(a[1][1]) + Swift.abs(a[2][2]) + 1e-300) { break }
        let theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
        let t = (theta >= 0 ? 1.0 : -1.0) / (Swift.abs(theta) + (theta * theta + 1).squareRoot())
        let c = 1 / (t * t + 1).squareRoot(), s = t * c
        for k in 0..<3 {
            let akp = a[k][p], akq = a[k][q]
            a[k][p] = c * akp - s * akq
            a[k][q] = s * akp + c * akq
        }
        for k in 0..<3 {
            let apk = a[p][k], aqk = a[q][k]
            a[p][k] = c * apk - s * aqk
            a[q][k] = s * apk + c * aqk
        }
        for k in 0..<3 {
            let vkp = v[k][p], vkq = v[k][q]
            v[k][p] = c * vkp - s * vkq
            v[k][q] = s * vkp + c * vkq
        }
    }
    let i = (0..<3).max { a[$0][$0] < a[$1][$1] }!
    return SIMD3(v[0][i], v[1][i], v[2][i])
}
