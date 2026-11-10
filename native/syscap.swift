// syscap — захват системного звука macOS (всё, что слышно в наушниках) через ScreenCaptureKit.
// Без BlackHole и «Многовыходного устройства»: работает с любыми наушниками, AirPods и проводными.
// Пишет в stdout сырой PCM: Int16, моно, 48000 Гц. Служебные сообщения — в stderr.
// Сборка: swiftc -O -swift-version 5 syscap.swift -o bin/syscap

import CoreMedia
import Foundation
import ScreenCaptureKit

let sampleRate = 48000

final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    private let out = FileHandle.standardOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, CMSampleBufferIsValid(sb),
              let fmt = CMSampleBufferGetFormatDescription(sb),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fmt) else { return }
        let asbd = asbdPtr.pointee
        let frames = CMSampleBufferGetNumSamples(sb)
        if frames <= 0 { return }

        // Достаём AudioBufferList из CMSampleBuffer
        var needed = 0
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: &needed, bufferListOut: nil, bufferListSize: 0,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil, flags: 0, blockBufferOut: nil)
        let raw = UnsafeMutableRawPointer.allocate(byteCount: needed, alignment: 16)
        defer { raw.deallocate() }
        let abl = raw.bindMemory(to: AudioBufferList.self, capacity: 1)
        var block: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: nil, bufferListOut: abl, bufferListSize: needed,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment, blockBufferOut: &block)
        guard status == noErr else { return }

        let buffers = UnsafeMutableAudioBufferListPointer(abl)
        let isFloat = asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0
        let nonInterleaved = asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
        let channels = max(1, Int(asbd.mChannelsPerFrame))
        var mix = [Float](repeating: 0, count: frames)

        // Сводим все каналы в моно
        if nonInterleaved {
            for buf in buffers {
                guard let data = buf.mData else { continue }
                if isFloat {
                    let p = data.assumingMemoryBound(to: Float.self)
                    for i in 0..<frames { mix[i] += p[i] }
                } else {
                    let p = data.assumingMemoryBound(to: Int16.self)
                    for i in 0..<frames { mix[i] += Float(p[i]) / 32768 }
                }
            }
            let n = Float(max(1, buffers.count))
            for i in 0..<frames { mix[i] /= n }
        } else if let data = buffers.first?.mData {
            if isFloat {
                let p = data.assumingMemoryBound(to: Float.self)
                for i in 0..<frames {
                    var s: Float = 0
                    for c in 0..<channels { s += p[i * channels + c] }
                    mix[i] = s / Float(channels)
                }
            } else {
                let p = data.assumingMemoryBound(to: Int16.self)
                for i in 0..<frames {
                    var s: Float = 0
                    for c in 0..<channels { s += Float(p[i * channels + c]) / 32768 }
                    mix[i] = s / Float(channels)
                }
            }
        }

        var pcm = [Int16](repeating: 0, count: frames)
        for i in 0..<frames {
            pcm[i] = Int16(max(-1, min(1, mix[i])) * 32767)
        }
        pcm.withUnsafeBytes { out.write(Data($0)) }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("error: \(error.localizedDescription)\n".data(using: .utf8)!)
        exit(2)
    }
}

let capture = Capture()
var activeStream: SCStream?

Task {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else {
            FileHandle.standardError.write("error: no display\n".data(using: .utf8)!)
            exit(3)
        }
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = sampleRate
        config.channelCount = 1
        // Видео не нужно: минимальный кадр раз в секунду
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let stream = SCStream(filter: filter, configuration: config, delegate: capture)
        try stream.addStreamOutput(capture, type: .audio, sampleHandlerQueue: DispatchQueue(label: "syscap.audio"))
        try stream.addStreamOutput(capture, type: .screen, sampleHandlerQueue: DispatchQueue(label: "syscap.video"))
        try await stream.startCapture()
        activeStream = stream
        FileHandle.standardError.write("ready\n".data(using: .utf8)!)
    } catch {
        FileHandle.standardError.write("error: \(error.localizedDescription)\n".data(using: .utf8)!)
        exit(1)
    }
}

// Родительский процесс закрыл stdin (или завершился) — выходим
DispatchQueue.global().async {
    while readLine() != nil {}
    exit(0)
}

dispatchMain()
