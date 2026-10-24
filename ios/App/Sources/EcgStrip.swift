// Tracciato ECG degli ultimi secondi, a scorrimento, con i picchi R marcati.

import PolarKit
import SwiftUI

struct EcgStrip: View {
    let snapshot: Snapshot

    var body: some View {
        Canvas { ctx, size in
            let y = snapshot.ecg
            guard y.count > 2 else { return }
            // Scala stabile: ±1.5 mV, allargata solo se il segnale è più ampio
            let peak = max(1500, y.map(abs).max() ?? 1500)
            let sx = size.width / CGFloat(y.count - 1)
            let sy = (size.height / 2 - 6) / CGFloat(peak)
            let mid = size.height / 2

            var grid = Path()
            for k in stride(from: 0.0, through: Double(size.width), by: Double(sx) * snapshot.ecgFs * 0.2) {
                grid.move(to: CGPoint(x: k, y: 0))
                grid.addLine(to: CGPoint(x: k, y: size.height))
            }
            ctx.stroke(grid, with: .color(.white.opacity(0.06)), lineWidth: 1)

            var path = Path()
            path.move(to: CGPoint(x: 0, y: mid - CGFloat(y[0]) * sy))
            for i in 1..<y.count {
                path.addLine(to: CGPoint(x: CGFloat(i) * sx, y: mid - CGFloat(y[i]) * sy))
            }
            ctx.stroke(path, with: .color(Theme.ecg), lineWidth: 1.6)

            let first = Double(snapshot.ecgEnd - y.count)
            for b in snapshot.beatIdx {
                let i = b - first
                guard i >= 0, i < Double(y.count) else { continue }
                let k = min(y.count - 1, Int(i.rounded()))
                let p = CGPoint(x: CGFloat(i) * sx, y: mid - CGFloat(y[k]) * sy)
                ctx.fill(Path(ellipseIn: CGRect(x: p.x - 3, y: p.y - 3, width: 6, height: 6)), with: .color(Theme.accent))
            }
        }
        .padding(.vertical, 6)
        .drawingGroup()
    }
}
