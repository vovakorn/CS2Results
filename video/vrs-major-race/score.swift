import Foundation
import AVFoundation
import AudioToolbox

struct NoteEvent: Decodable {
    let seconds: Double
    let instrument: String
    let note: UInt8
    let velocity: UInt8
    let on: Bool
}
struct Score: Decodable {
    let duration: Double
    let instruments: [String: UInt8]
    let gains: [String: Float]?
    let pans: [String: Float]?
    let events: [NoteEvent]
}

let args = CommandLine.arguments
guard args.count == 3 else { fatalError("score.swift score.json output.wav") }
let score = try JSONDecoder().decode(Score.self, from: Data(contentsOf: URL(fileURLWithPath: args[1])))
let engine = AVAudioEngine()
let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2)!
let bank = URL(fileURLWithPath: "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls")
var samplers: [String: AVAudioUnitSampler] = [:]
for (name, program) in score.instruments.sorted(by: {$0.key < $1.key}) {
    let sampler = AVAudioUnitSampler()
    engine.attach(sampler)
    engine.connect(sampler, to: engine.mainMixerNode, format: format)
    try sampler.loadSoundBankInstrument(at: bank, program: program,
        bankMSB: UInt8(kAUSampler_DefaultMelodicBankMSB), bankLSB: UInt8(kAUSampler_DefaultBankLSB))
    sampler.masterGain = score.gains?[name] ?? (name == "strings" ? -13 : name == "bass" ? -10 : name == "timpani" ? -8 : -10)
    sampler.stereoPan = score.pans?[name] ?? (name == "trumpet" ? 18 : name == "horns" ? -20 : 0)
    samplers[name] = sampler
}
let reverb = AVAudioUnitReverb()
engine.attach(reverb)
reverb.loadFactoryPreset(.largeHall)
reverb.wetDryMix = 15
engine.connect(engine.mainMixerNode, to: reverb, format: format)
engine.connect(reverb, to: engine.outputNode, format: format)
engine.mainMixerNode.outputVolume = 0.78
try engine.enableManualRenderingMode(.offline, format: format, maximumFrameCount: 1024)
try engine.start()
let output = try AVAudioFile(forWriting: URL(fileURLWithPath: args[2]), settings: format.settings)
let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 1024)!
let events = score.events.sorted { a, b in
    a.seconds == b.seconds ? (!a.on && b.on) : a.seconds < b.seconds
}
let total = Int64((score.duration * 48000).rounded())
var next = 0
while engine.manualRenderingSampleTime < total {
    let now = engine.manualRenderingSampleTime
    while next < events.count && Int64((events[next].seconds * 48000).rounded()) <= now {
        let event = events[next]
        if event.on { samplers[event.instrument]!.startNote(event.note, withVelocity: event.velocity, onChannel: 0) }
        else { samplers[event.instrument]!.stopNote(event.note, onChannel: 0) }
        next += 1
    }
    let target = next < events.count ? min(total, Int64((events[next].seconds * 48000).rounded())) : total
    let count = AVAudioFrameCount(min(1024, max(1, target - now)))
    switch try engine.renderOffline(count, to: buffer) {
    case .success: try output.write(from: buffer)
    case .cannotDoInCurrentContext: continue
    default: fatalError("Offline audio render failed")
    }
}
engine.stop()
print("Rendered sampled brass / strings / timpani: \(score.duration)s")
