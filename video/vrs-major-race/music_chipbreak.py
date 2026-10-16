"""Original chipbreak cue: pulse melody, triangle bass and chopped drums."""
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
BPM = 180
LABEL = 'Original 180 BPM chipbreak: pulse lead, arpeggios, triangle bass and chopped breakbeats'
REFERENCE = {'artist':'KOLA KID','track':'Pizza Power','usage':'Sonic reference; original composition, not the reference recording'}


def signature(duration):
    return hashlib.sha256(Path(__file__).read_bytes()+str(duration).encode()).hexdigest()


def render(duration=32.4):
    try:
        import numpy as np
    except ImportError:
        # The desktop app supplies NumPy independently of the bot's dependencies.
        runtime = Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/lib'
        version = f'python{sys.version_info.major}.{sys.version_info.minor}'
        sys.path.insert(0,str(runtime/version/'site-packages'))
        import numpy as np
    import imageio_ffmpeg
    length = round(duration*RATE)
    drums = np.zeros((length,2),dtype=np.float64)
    tonal = np.zeros_like(drums)
    kick_envelope = np.zeros(length)
    rng = np.random.default_rng(73419)
    beat = 60/BPM
    events = []

    def add(bus,sound,seconds,amplitude=1,pan=0):
        start = round(seconds*RATE)
        if start >= length: return
        end = min(length,start+len(sound))
        sound = sound[:end-start]*amplitude
        left,right = np.sqrt((1-pan)/2),np.sqrt((1+pan)/2)
        bus[start:end,0] += sound*left
        bus[start:end,1] += sound*right

    def clock(seconds):
        return np.arange(max(1,round(seconds*RATE)))/RATE

    def edge(phase,step):
        out = np.zeros_like(phase)
        early = phase<step
        x = phase[early]/step[early]
        out[early] = x+x-x*x-1
        late = phase>1-step
        x = (phase[late]-1)/step[late]
        out[late] = x*x+x+x+1
        return out

    def pulse(phase,step,duty):
        return np.where(phase<duty,1.,-1.) + edge(phase,step) - edge((phase-duty)%1,step)

    def tone(midi,seconds,voice='lead',variant=0):
        t = clock(seconds)
        frequency = 440*2**((midi-69)/12)
        vibrato = 1 + .002*np.sin(2*np.pi*5.8*t)*np.clip((t-.07)/.13,0,1)
        step = np.full_like(t,frequency/RATE)*vibrato
        phase = np.cumsum(step)%1
        if voice=='bass':
            triangle = 2/np.pi*np.arcsin(np.sin(2*np.pi*phase))
            sound = .8*triangle+.2*pulse(phase,step,.5)
            envelope = np.minimum(1,t/.004)*np.exp(-t*1.8)
        elif voice=='arp':
            sound = .7*pulse(phase,step,.25)+.3*np.sin(2*np.pi*phase)
            envelope = np.minimum(1,t/.002)*np.exp(-t*9)
        elif voice=='stab':
            sound = .65*pulse(phase,step,.375)+.35*np.sin(2*np.pi*phase)
            envelope = np.minimum(1,t/.003)*np.exp(-t*7)
        else:
            sound = .7*pulse(phase,step,.125 if variant%2==0 else .25)+.3*np.sin(2*np.pi*phase)
            envelope = np.minimum(1,t/.003)*(.72+.28*np.exp(-t*12))
        envelope *= np.minimum(1,np.maximum(0,seconds-t)/min(.023,seconds/5))
        return sound*envelope

    def noise(seconds,lo=0,hi=24000):
        n = len(clock(seconds))
        source = rng.normal(size=n)
        bins = np.fft.rfftfreq(n,1/RATE)
        shaped = np.fft.irfft(np.fft.rfft(source)*((bins>=lo)&(bins<=hi)),n)
        return shaped/max(.001,np.sqrt(np.mean(shaped*shaped)))

    t=clock(.24)
    phase=2*np.pi*(49*t+72*.019*(1-np.exp(-t/.019)))
    kick=np.tanh(1.7*np.sin(phase))*np.exp(-t*20)+.18*noise(.24,3500,14000)*np.exp(-t*250)
    kick*=np.minimum(1,t/.001)
    t=clock(.22)
    snare=(.57*noise(.22,900,15000)*np.exp(-t*25)+.28*np.sin(2*np.pi*185*t)*np.exp(-t*30)
           +.13*np.sin(2*np.pi*340*t)*np.exp(-t*45))
    snare=np.tanh(snare*1.2)*np.minimum(1,t/.0008)
    t=clock(.085)
    hat=noise(.085,6500,17000)*np.exp(-t*75)*np.minimum(1,t/.0005)
    t=clock(.30)
    open_hat=noise(.30,6000,18000)*np.exp(-t*14)*np.minimum(1,t/.0007)
    t=clock(.70)
    crash=(noise(.70,3000,18000)*np.exp(-t*5))*np.minimum(1,t/.001)

    chords=[[57,60,64],[53,57,60],[60,64,67],[55,59,62]]
    # Original call-and-response hook, with a different answer each two bars.
    hooks=[[(0,76,.5),(.75,79,.25),(1,81,.5),(1.75,79,.25),(2,76,.5),(2.75,72,.25),(3,74,.5),(3.5,76,.5)],
           [(0,81,.75),(.75,79,.25),(1,76,.5),(1.75,72,.25),(2,74,.5),(2.75,76,.25),(3,79,.5),(3.5,76,.5)],
           [(0,72,.5),(.75,76,.25),(1,79,.75),(2,76,.5),(2.5,74,.5),(3.25,72,.25),(3.5,74,.5)],
           [(0,74,.75),(.75,79,.25),(1,83,.5),(1.75,81,.25),(2,79,.5),(2.75,74,.25),(3,76,.5),(3.5,79,.5)]]
    for bar in range(24):
        at=bar*4*beat
        chord=chords[(bar//2)%4]
        lift = bar>=21
        if lift:
            chord=[[57,61,64],[53,57,60],[55,59,62]][bar-21]
        breakdown = bar==20
        for step in range(16):
            moment=at+step*beat/4
            if bar==23 and step>=12:continue
            pitch=chord[(step+bar)%3]+12+(12 if step%4==3 else 0)
            add(tonal,tone(pitch,beat*.23,'arp'),moment,.080 if not breakdown else .065,pan=-.42)
            if not breakdown:
                add(drums,hat,moment+(0.008 if step%2 else 0),.087 if step%2 else .125,pan=.26)
        for step in [0,.75,1.5,2,2.75,3.5]:
            if bar==23 and step>=3:continue
            add(tonal,tone(chord[0]-24 if step in [0,2] else chord[0]-12,beat*.42,'bass'),at+step*beat,.27)
        for step in [1.5,3.25]:
            for index,pitch in enumerate(chord):
                add(tonal,tone(pitch+12,beat*.36,'stab'),at+step*beat,.040,pan=.24+index*.1)
        hook=hooks[(bar//2)%4]
        for position,pitch,gate in hook:
            if bar==23 and position>=3:continue
            if bar%2 and position==3.5:pitch+=3 if not lift else 4
            if lift and pitch in [72,79,81]:pitch+=1 if pitch==72 else 0
            sound=tone(pitch+(12 if bar in [17,19,22] else 0),gate*beat*.90,variant=bar)
            add(tonal,sound,at+position*beat,.18 if not breakdown else .13,pan=-.03)
            add(tonal,sound,at+position*beat+beat*.75,.036,pan=.56)
            add(tonal,sound,at+position*beat+beat*1.5,.018,pan=-.56)
            events.append({'seconds':round(at+position*beat,5),'voice':'pulse','note':pitch,'gate':round(gate*beat*.90,5)})
        if breakdown:
            add(drums,open_hat,at,.12,pan=.3)
            continue
        kicks=[0,.75,2.5] if bar%2==0 else [0,1.75,2.5,3.5]
        for position in kicks:
            if bar==23 and position>=3:continue
            moment=at+position*beat
            add(drums,kick,moment,.70 if position in [0,2.5] else .48)
            start=round(moment*RATE);end=min(length,start+round(.12*RATE))
            kick_envelope[start:end]=np.maximum(kick_envelope[start:end],np.exp(-np.arange(end-start)/(RATE*.032)))
        for position,level in [(1,.57),(3,.62),(.75,.10),(1.5,.14),(2.75,.13)]:
            if bar==23 and position>=3:continue
            add(drums,snare,at+position*beat,level)
        if bar%4==3:
            for position,level in [(3.25,.20),(3.5,.30),(3.75,.42),(3.875,.25)]:
                if bar==23:continue
                add(drums,snare[:round(.07*RATE)],at+position*beat,level,pan=-.18 if position==3.5 else .12)
        if bar%4==0 or bar==21:add(drums,crash,at,.085,pan=.35)
        if bar%2:add(drums,open_hat,at+2.5*beat,.085,pan=.35)
    # A short rising pickup accompanies the camera opening the full history.
    t=clock(.62)
    riser=noise(.62,2500,13000)*(t/.62)**2*.10
    add(drums,riser,26.40,pan=-.1)
    final_at=31.0
    tail = np.arange(length-round(final_at*RATE))/RATE
    tonal[round(final_at*RATE):] *= np.exp(-tail/.028)[:,None]
    add(drums,kick,final_at,.72)
    add(drums,crash,final_at,.15,pan=.15)
    for pitch in [69,73,76,81]:
        add(tonal,tone(pitch,duration-final_at,'stab'),final_at,.085,pan=(pitch-75)/30)
    add(tonal,tone(33,duration-final_at,'bass'),final_at,.25)
    tonal*= (1-.34*kick_envelope)[:,None]
    mix=drums+tonal
    mix=np.tanh(mix*1.20)
    global_time=np.arange(length)/RATE
    mix*=np.minimum(1,global_time/.008)[:,None]
    mix*=np.minimum(1,(duration-global_time)/.24)[:,None]
    mix*=.92/max(.001,np.max(np.abs(mix)))
    raw=PUBLIC/'chipbreak-raw.wav'
    with wave.open(str(raw),'wb') as output:
        output.setnchannels(2);output.setsampwidth(2);output.setframerate(RATE)
        output.writeframes((mix*32767).astype('<i2').tobytes())
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    target=PUBLIC/'original-soundtrack.wav'
    measured=subprocess.run([ffmpeg,'-hide_banner','-i',str(raw),'-af',
        'loudnorm=I=-14:TP=-1.5:LRA=7:print_format=json','-f','null','-'],capture_output=True,text=True,check=True)
    stats=json.loads(measured.stderr[measured.stderr.rfind('{'):measured.stderr.rfind('}')+1])
    normalize=('loudnorm=I=-14:TP=-1.5:LRA=7:linear=true'
        f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
        f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
        f":offset={stats['target_offset']}")
    subprocess.run([ffmpeg,'-y','-v','error','-i',str(raw),'-af',normalize,
        '-ar',str(RATE),'-ac','2','-c:a','pcm_s16le',str(target)],check=True)
    metadata={'label':LABEL,'reference':REFERENCE,'tempo':BPM,'duration':duration,'rate':RATE,
              'channels':2,'source':'Original oscillators and generated drum sounds; no external audio samples',
              'arrangement':'Original hook, breakbeat variations, one-bar pickup, final lift and resolve',
              'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'loudnessTargetLufs':-14,'truePeakTargetDb':-1.5}
    (PUBLIC/'music-manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    (PUBLIC/'chipbreak-score.json').write_text(json.dumps({'metadata':metadata,'leadEvents':events},indent=2))
    (PUBLIC/'music-signature.txt').write_text(signature(duration))
    return target


if __name__=='__main__':
    print(render())
