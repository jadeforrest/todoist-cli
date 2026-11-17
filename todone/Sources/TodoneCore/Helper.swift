import Foundation

public enum HelperError: Error, LocalizedError, Equatable {
    /// The helper ran the request and refused it; the message is for the user.
    case remote(String)
    case malformed(String)
    case exited(Int32)

    public var errorDescription: String? {
        switch self {
        case .remote(let message): return message
        case .malformed(let detail): return "Unreadable reply from the helper: \(detail)"
        case .exited(let status): return "The Todone helper stopped (exit \(status)). Restart the app."
        }
    }
}

/// Splits a byte stream into newline-terminated lines.
public struct LineBuffer {
    private var pending = Data()

    public init() {}

    public mutating func append(_ data: Data) -> [Data] {
        pending.append(data)
        var lines: [Data] = []
        while let newline = pending.firstIndex(of: 0x0A) {
            lines.append(pending[pending.startIndex..<newline])
            pending.removeSubrange(pending.startIndex...newline)
        }
        return lines
    }
}

public enum Protocol {
    public static func request(id: Int, method: String, params: [String: Any]) throws -> Data {
        var data = try JSONSerialization.data(withJSONObject: ["id": id, "method": method, "params": params])
        data.append(0x0A)
        return data
    }

    /// The response's id and either its `result` (re-encoded as JSON) or its error.
    public static func response(_ line: Data) -> (id: Int?, result: Result<Data, HelperError>) {
        guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
            return (nil, .failure(.malformed(String(decoding: line, as: UTF8.self))))
        }
        let id = object["id"] as? Int
        if let message = object["error"] as? String {
            return (id, .failure(.remote(message)))
        }
        guard let result = object["result"],
              let data = try? JSONSerialization.data(withJSONObject: result, options: .fragmentsAllowed)
        else {
            return (id, .failure(.malformed("no result")))
        }
        return (id, .success(data))
    }
}

/// The running `python -m <module>` helper process, called one JSON line at a time.
public final class Helper: @unchecked Sendable {
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private let lock = NSLock()
    private var buffer = LineBuffer()
    private var pending: [Int: CheckedContinuation<Data, Error>] = [:]
    private var nextId = 1
    private var exitStatus: Int32?

    public init(python: URL, module: String, arguments: [String], environment: [String: String]) throws {
        process.executableURL = python
        process.arguments = ["-m", module] + arguments
        process.environment = environment
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.standardError
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.receive(handle.availableData)
        }
        process.terminationHandler = { [weak self] proc in
            self?.failAll(status: proc.terminationStatus)
        }
        try process.run()
    }

    public func stop() {
        if process.isRunning { process.terminate() }
    }

    public func call(_ method: String, _ params: [String: Any] = [:]) async throws -> Data {
        let (id, exited): (Int, Int32?) = lock.withLock {
            defer { nextId += 1 }
            return (nextId, exitStatus)
        }
        if let exited { throw HelperError.exited(exited) }
        let line = try Protocol.request(id: id, method: method, params: params)
        return try await withCheckedThrowingContinuation { continuation in
            lock.withLock { pending[id] = continuation }
            input.fileHandleForWriting.write(line)
        }
    }

    public func call<T: Decodable>(_ method: String, _ params: [String: Any] = [:], as type: T.Type) async throws -> T {
        try Decoding.decode(type, from: try await call(method, params))
    }

    private func receive(_ data: Data) {
        let lines = lock.withLock { buffer.append(data) }
        for line in lines {
            let (id, result) = Protocol.response(line)
            guard let id, let continuation = lock.withLock({ pending.removeValue(forKey: id) }) else { continue }
            continuation.resume(with: result.mapError { $0 as Error })
        }
    }

    private func failAll(status: Int32) {
        let waiting = lock.withLock {
            exitStatus = status
            defer { pending.removeAll() }
            return Array(pending.values)
        }
        waiting.forEach { $0.resume(throwing: HelperError.exited(status)) }
    }
}
