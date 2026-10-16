import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {VRSReel} from './VRSReel.jsx';
import {FPS, frameCount} from './timeline.mjs';
import regions from './public/regional-data.json';

const Root = () => <>{regions.map((data) => <Composition key={data.regionSlug}
  id={`Vrs-${data.regionSlug}`} component={VRSReel} width={1080} height={1920}
  fps={FPS} durationInFrames={frameCount(data)} defaultProps={{data}} />)}</>;
registerRoot(Root);
