"""Original tactical orchestral cue with the CS2 main theme as a mood reference."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import wave

HERE = Path(__file__).resolve().parent
PUBLIC = HERE / 'public'
PUBLIC.mkdir(exist_ok=True)
RATE = 48000
BPM = 400 / 3  # A 4/4 bar per 1.8-second ranking transition.
LABEL = 'Road to Major: original tactical orchestral theme, 133.33 BPM'
VARIANT = 'cs2-theme'
REFERENCE = {
    'artist':'Valve Studio Orchestra', 'track':'Counter-Strike 2',
    'url':'https://store.steampowered.com/app/2678630/CounterStrike_2_Soundtrack/',
    'usage':'Mood and instrumentation reference; original melody and arrangement; no reference recording used',
}


def signature(duration):
    return hashlib.sha256(Path(__file__).read_bytes() + (HERE/'score.swift').read_bytes()
                          + str(duration).encode()).hexdigest()


def make_score(duration):
    beat = 60/BPM
    events = []

    def note(voice, pitch, bar, position, beats, velocity):
        at = (bar*4+position)*beat
        if at >= duration:
            return
        events.extend([
            {'seconds':round(at,6),'instrument':voice,'note':pitch,'velocity':velocity,'on':True},
            {'seconds':round(min(duration-.025,at+beats*beat),6),
             'instrument':voice,'note':pitch,'velocity':0,'on':False},
        ])

    # D minor, Bb, G minor, A. The original melody stays below piercing trumpet range.
    harmony = [(50,[62,65,69]),(46,[58,62,65]),(43,[58,62,67]),(45,[57,61,64])]
    phrases = [
        [(0,62,1.25),(1.5,69,.4),(2,67,.6),(2.75,65,.4),(3.25,64,.6)],
        [(0,62,.75),(1,65,.75),(2,70,1.1),(3.25,69,.6)],
        [(0,67,1.1),(1.25,65,.5),(2,62,.8),(3,58,.7)],
        [(0,61,.65),(.75,64,.65),(1.75,69,.8),(3,64,.65)],
        [(0,74,.9),(1.25,69,.45),(2,72,.9),(3.25,69,.5)],
        [(0,70,.7),(1,69,.7),(2,65,1.45)],
        [(0,67,.85),(1.25,70,.5),(2,69,.75),(3,67,.7)],
        [(0,64,.6),(.75,61,.6),(1.75,64,.6),(2.5,69,1.1)],
    ]
    for bar in range(17):
        root,chord = harmony[(bar-1)%4] if bar else harmony[0]
        if bar == 15:
            root,chord = harmony[3]
        if bar == 16:
            root,chord = harmony[0]
        for pitch in chord:
            note('sustain',pitch,bar,.08,3.65,49 if bar<9 else 61)
        note('bass',root-12,bar,0,1.75,85)
        note('bass',root-12,bar,2,1.72,77)
        pattern = [root,root+7,root+12,root+7,root,root+7,chord[1],root+7]
        for step,pitch in enumerate(pattern):
            note('strings',pitch,bar,step/2,.38,(83 if step in [0,4] else 69)+(7 if bar>=9 else 0))
            if bar>=9:
                note('pizz',pitch+12,bar,step/2+.015,.24,56 if step%2 else 66)
        if bar==0:
            for pitch in [50,57,62]:
                note('horns',pitch,bar,.08,3.4,69)
        else:
            phrase = phrases[(bar-1)%len(phrases)]
            if bar==15:
                phrase = [(0,61,.65),(1,64,.65),(2,67,.65),(3,69,.65)]
            if bar==16:
                phrase = [(0,74,1.2),(1.5,72,.4),(2,69,.8),(3,65,.7)]
            for pos,pitch,gate in phrase:
                note('horns',pitch,bar,pos,gate,89 if bar>=9 else 80)
                if bar>=9 and pos in [0,2]:
                    note('trumpet',pitch,bar,pos+.025,gate*.86,66)
        for pos in [0,2.5]:
            for pitch in [root,chord[1]-12,chord[2]-12]:
                note('trombone',pitch,bar,pos,.65,78 if bar>=9 else 67)
        if bar%4==0 or bar in [15,16]:
            note('timpani',root-12,bar,0,.8,86)
        if bar in [4,8,12,15]:
            for step in range(4):
                note('timpani',root-12+(7 if step%2 else 0),bar,3+step/4,.22,60+step*9)
    # A final D chord with a Picardy third, followed by 1.8 seconds of release.
    for pitch in [50,57,62,66,69]:
        note('horns',pitch,17,0,2.85,87)
        note('sustain',pitch+12,17,.02,3.1,70)
    note('bass',38,17,0,3.2,94)
    note('timpani',38,17,0,.8,96)
    return {
        'duration':duration,
        'instruments':{'horns':60,'trumpet':56,'trombone':57,'strings':48,
                       'sustain':49,'pizz':45,'bass':43,'timpani':47},
        'gains':{'horns':-7,'trumpet':-19,'trombone':-17,'strings':-15,
                 'sustain':-24,'pizz':-22,'bass':-15,'timpani':-12},
        'pans':{'horns':-8,'trumpet':16,'trombone':-16,'strings':-26,
                'sustain':28,'pizz':28,'bass':0,'timpani':-9},
        'events':events,
    }


def render(duration=32.4):
    if abs(duration-32.4)>.001:
        raise ValueError('This cue is arranged for the 32.4-second VRS timeline')
    try:
        import numpy as np
    except ImportError:
        runtime = Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/lib'
        sys.path.insert(0,str(runtime/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'))
        import numpy as np
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    score_path = PUBLIC/'cs2-theme-score.json'
    score_path.write_text(json.dumps(make_score(duration),indent=2))
    orchestra = PUBLIC/'cs2-theme-orchestra.wav'
    subprocess.run(['swift','-module-cache-path','/private/tmp/cs2-vrs-music-module-cache',
                    str(HERE/'score.swift'),str(score_path),str(orchestra)],check=True)
    decoded = subprocess.run([ffmpeg,'-v','error','-i',str(orchestra),'-f','f32le',
                              '-ar',str(RATE),'-ac','2','-'],check=True,capture_output=True).stdout
    mix = np.frombuffer(decoded,dtype='<f4').reshape((-1,2)).astype(np.float64)
    length = round(duration*RATE)
    assert len(mix)==length
    rng = np.random.default_rng(91682)
    beat = 60/BPM
    drums = np.zeros_like(mix)
    synth = np.zeros_like(mix)

    def clock(seconds):
        return np.arange(round(seconds*RATE))/RATE

    def add(bus,sound,at,level,pan=0):
        start = round(at*RATE)
        if start>=length:
            return
        end = min(length,start+len(sound))
        bus[start:end] += (sound[:end-start]*level)[:,None]*np.array([np.sqrt((1-pan)/2),np.sqrt((1+pan)/2)])

    def noise(seconds,low,high):
        n = len(clock(seconds))
        freqs = np.fft.rfftfreq(n,1/RATE)
        spectrum = np.fft.rfft(rng.normal(size=n))
        sound = np.fft.irfft(spectrum*((freqs>=low)&(freqs<=high)),n)
        return sound/max(.001,np.sqrt(np.mean(sound*sound)))

    t = clock(.48)
    kick = (.85*np.sin(2*np.pi*(43*t+40*.032*(1-np.exp(-t/.032))))*np.exp(-t*15)
            +.07*noise(.48,1800,8000)*np.exp(-t*170))*np.minimum(1,t/.001)
    t = clock(.25)
    snare = (.46*noise(.25,1300,11000)*np.exp(-t*24)+.20*np.sin(2*np.pi*188*t)*np.exp(-t*27)
             +.09*np.sin(2*np.pi*342*t)*np.exp(-t*35))*np.minimum(1,t/.0008)
    t = clock(.1)
    hat = noise(.1,6500,16000)*np.exp(-t*65)*np.minimum(1,t/.001)
    t = clock(1.8)
    cymbal = noise(1.8,2800,16000)*np.exp(-t*3.6)*np.minimum(1,t/.004)
    t = clock(.6)
    low_tom = (np.sin(2*np.pi*(75*t+14*.08*(1-np.exp(-t/.08))))
               +.28*np.sin(2*np.pi*118*t))*np.exp(-t*12)*np.minimum(1,t/.001)
    for bar in range(17):
        at = bar*4*beat
        root = [38,34,31,33][(bar-1)%4] if bar else 38
        if bar==15:
            root=33
        if bar==16:
            root=38
        intensity = .78 if bar<5 else .91 if bar<13 else 1.0
        for pos,level in [(0,.39),(1.5,.17),(2,.32),(3.5,.19)]:
            add(drums,kick,at+pos*beat,level*intensity)
        for pos,level in [(1,.33),(3,.37),(.75,.055),(2.75,.07)]:
            add(drums,snare,at+pos*beat,level*intensity,pan=.06)
        for step in range(8):
            add(drums,hat,at+step*beat/2,.035 if step%2 else .045,pan=.32)
            t = clock(beat*.43)
            freq = 440*2**((root-69)/12)
            pulse = (np.sin(2*np.pi*freq*t)+.30*np.sin(2*np.pi*2*freq*t)+.13*np.sin(2*np.pi*3*freq*t))
            pulse *= np.minimum(1,t/.008)*np.exp(-t*7)*np.minimum(1,(len(t)/RATE-t)/.025)
            add(synth,pulse,at+step*beat/2,.080 if step%4==0 else .052)
        if bar in [3,7,11,14,15]:
            for step in range(8):
                add(drums,snare,at+(3+step/8)*beat,.055+step*.017,pan=(-.1 if step%2 else .1))
            add(drums,low_tom,at+3.5*beat,.18,pan=-.3)
            add(drums,low_tom,at+3.75*beat,.21,pan=.25)
        if bar in [0,1,5,9,13,16]:
            add(drums,cymbal,at,.072,pan=.25)
    t = clock(1.25)
    add(drums,noise(1.25,1800,12000)*(t/1.25)**2,25.75,.065,pan=.14)
    add(drums,kick,30.6,.48)
    add(drums,cymbal,30.6,.115,pan=.18)
    add(drums,low_tom,30.6,.18,pan=-.12)
    room = drums.copy()
    for delay,gain,reverse in [(.027,.18,True),(.061,.10,False),(.109,.065,True)]:
        shift = round(delay*RATE)
        reflected = drums[:-shift,::-1] if reverse else drums[:-shift]
        room[shift:] += reflected*gain
    mix = np.tanh((mix+room+synth)*1.12)
    t = np.arange(length)/RATE
    mix *= np.minimum(1,t/.012)[:,None]
    mix *= np.minimum(1,(duration-t)/.34)[:,None]
    mix *= .90/max(.001,float(np.max(np.abs(mix))))
    raw = PUBLIC/'cs2-theme-raw.wav'
    with wave.open(str(raw),'wb') as out:
        out.setnchannels(2);out.setsampwidth(2);out.setframerate(RATE)
        out.writeframes((mix*32767).astype('<i2').tobytes())
    measured = subprocess.run([ffmpeg,'-hide_banner','-i',str(raw),'-af',
        'loudnorm=I=-14:TP=-2:LRA=7:print_format=json','-f','null','-'],capture_output=True,text=True,check=True)
    stats = json.loads(measured.stderr[measured.stderr.rfind('{'):measured.stderr.rfind('}')+1])
    normalize = ('loudnorm=I=-14:TP=-2:LRA=7:linear=true'
                 f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
                 f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
                 f":offset={stats['target_offset']}")
    target = PUBLIC/'original-soundtrack.wav'
    subprocess.run([ffmpeg,'-y','-v','error','-i',str(raw),'-af',normalize,
                    '-ar',str(RATE),'-ac','2','-c:a','pcm_s16le',str(target)],check=True)
    metadata = {'label':LABEL,'variant':VARIANT,'reference':REFERENCE,'tempo':round(BPM,6),
                'duration':duration,'rate':RATE,'channels':2,
                'source':'Original score rendered with macOS General MIDI orchestral samples; original synthesized drums and bass; no CS2 audio samples',
                'arrangement':'Low string ostinato, French horn theme, brass accents, marching snare, electronic bass; reveal build at 27.0s and final D chord at 30.6s',
                'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
                'loudnessTargetLufs':-14,'truePeakTargetDb':-2}
    (PUBLIC/'music-manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    (PUBLIC/'music-signature.txt').write_text(signature(duration))
    return target


if __name__=='__main__':
    print(render())
