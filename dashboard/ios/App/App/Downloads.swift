import UIKit
import WebKit
import OtoDockShell

/// Files into the app's Documents folder, which the Files app shows under
/// On My iPhone, OtoDock: an HTTP download the web view handed over (its
/// name from the address's `fn` value, else the server's), and a base64
/// payload the page posts on the channel (the lightbox's image types and
/// the diagnostics' JSON, under the saved-file rules). A toast reports each.
final class Downloads: NSObject, WKDownloadDelegate {

    private weak var shell: ShellViewController?
    private var active: [WKDownload] = []

    init(shell: ShellViewController) {
        self.shell = shell
    }

    static var documents: URL {
        return FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    func adopt(_ download: WKDownload) {
        download.delegate = self
        active.append(download)
    }

    // MARK: - WKDownloadDelegate

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let requested = URLComponents(url: response.url ?? URL(fileURLWithPath: "/"), resolvingAgainstBaseURL: false)?
            .queryItems?.first { $0.name == "fn" }?.value
        let name = Self.plainName(requested?.isEmpty == false ? requested! : suggestedFilename)
        completionHandler(Self.uniqueURL(for: name))
    }

    func downloadDidFinish(_ download: WKDownload) {
        active.removeAll { $0 === download }
        let name = download.progress.fileURL?.lastPathComponent ?? "the file"
        Toast.show("Saved \(name) to Files", in: shell?.view.window)
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        active.removeAll { $0 === download }
        Toast.show("Download failed", in: shell?.view.window)
    }

    // MARK: - The base64 save

    /// Only the types the lightbox and the diagnostics save, under a name
    /// whose extension comes from the type, at most 25 MB.
    func saveBase64(_ base64: String?, name: String?, mime: String?) {
        guard let ext = NativeBridgePolicy.saveExtension(mime), let base64, NativeBridgePolicy.saveBase64Fits(base64.utf16.count) else {
            Toast.show("This file can't be saved", in: shell?.view.window)
            return
        }
        let safeName = NativeBridgePolicy.saveFileName(name, ext)
        let window = shell?.view.window
        DispatchQueue.global(qos: .userInitiated).async {
            var payload = base64
            if payload.hasPrefix("data:"), let comma = payload.firstIndex(of: ",") {
                payload = String(payload[payload.index(after: comma)...])
            }
            guard let bytes = Data(base64Encoded: payload, options: .ignoreUnknownCharacters) else {
                DispatchQueue.main.async { Toast.show("Download failed: invalid image data", in: window) }
                return
            }
            guard bytes.count <= NativeBridgePolicy.maxSaveBytes else {
                DispatchQueue.main.async { Toast.show("This file can't be saved", in: window) }
                return
            }
            let target = Self.uniqueURL(for: safeName)
            do {
                try bytes.write(to: target, options: .atomic)
                DispatchQueue.main.async { Toast.show("Saved \(target.lastPathComponent) to Files", in: window) }
            } catch {
                ShellViewController.log.error("save failed: \((error as NSError).code)")
                DispatchQueue.main.async { Toast.show("Download failed. Try again.", in: window) }
            }
        }
    }

    // MARK: - Names

    /// The last path segment, reduced to plain characters, never hidden.
    static func plainName(_ requested: String) -> String {
        var base = requested
        if let cut = base.lastIndex(where: { $0 == "/" || $0 == "\\" }) { base = String(base[base.index(after: cut)...]) }
        while let first = base.first, first == "." || first.isWhitespace { base.removeFirst() }
        base = String(base.map { c -> Character in
            (c.isASCII && (c.isLetter || c.isNumber)) || c == " " || c == "_" || c == "." || c == "-" ? c : "_"
        }).trimmingCharacters(in: .whitespaces)
        if base.count > 120 { base = String(base.suffix(120)) }
        return base.isEmpty ? "download" : base
    }

    /// `name`, or `name (n)` when taken, in Documents.
    static func uniqueURL(for name: String) -> URL {
        let folder = documents
        let ext = (name as NSString).pathExtension
        let stem = ext.isEmpty ? name : (name as NSString).deletingPathExtension
        var candidate = folder.appendingPathComponent(name)
        var n = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            let numbered = ext.isEmpty ? "\(stem) (\(n))" : "\(stem) (\(n)).\(ext)"
            candidate = folder.appendingPathComponent(numbered)
            n += 1
        }
        return candidate
    }
}
