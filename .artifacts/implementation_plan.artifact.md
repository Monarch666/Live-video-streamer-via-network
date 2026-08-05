# High FPS Optimization for Global Bridge

This plan addresses the low 1 FPS issue by optimizing data throughput and relaxing congestion constraints for high-latency long-distance connections.

## User Review Required

> [!IMPORTANT]
> I will be reducing the default streaming resolution to **320x240** for Global Mode. This significantly reduces data size, which is the only way to reach 10-15 FPS on a free Cloudflare tunnel over long distances.

## Proposed Changes

### 1. Android App High-Speed Mode (`js/app.js`)

#### [MODIFY] `_sendFrames` logic
- **Reduce Data Weight**: Drop resolution to 320x240 and JPEG quality to 0.35.
- **Increase Buffer**: Raise the `bufferedAmount` threshold from 256KB to **1MB**. This allows more frames to be "in flight" across the globe simultaneously.
- **Eager Loop**: Change `setTimeout` to **10ms** (effectively "as fast as possible") as long as the 1MB buffer isn't full. The network will naturally pace the FPS.

### 2. UI Feedback
- Update the status text on the phone to show "Sending..." vs "Buffered" so you can see if the network is the bottleneck.

## Verification Plan

### Manual Verification
1. Rebuild and install APK.
2. Test on mobile data.
3. Observe the FPS counter on the PC. It should now reach 10-15 FPS even on moderate signals.
