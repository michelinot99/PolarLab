#!/bin/bash
# Controllo dei tipi dei sorgenti dell'app iPhone sul Mac, senza Xcode.
# I Command Line Tools non hanno le macro di SwiftUI: nella copia `@State` diventa un segnaposto.
set -e
cd "$(dirname "$0")"
rm -rf Sources && mkdir Sources
cp ../App/Sources/*.swift Sources/
sed -i '' 's/@State /@ShimState /' Sources/*.swift
cat > Sources/Shim.swift <<'SWIFT'
import SwiftUI
@propertyWrapper
struct ShimState<Value>: DynamicProperty {
    private let value: Value
    init(wrappedValue: Value) { value = wrappedValue }
    var wrappedValue: Value {
        get { value }
        nonmutating set {}
    }
    var projectedValue: Binding<Value> { .constant(value) }
}
SWIFT
swift build --scratch-path build.nosync 2>&1 | grep -E "error|warning|Build complete" | sed 's/.*AppCheck\/Sources\///' | sort -u
