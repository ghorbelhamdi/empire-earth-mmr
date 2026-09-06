'use strict';
window.captureGame = async () => {
  let stream;
  try {
    window.gameCapture.progress('Requesting the game window');
    stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 1 }, audio: false });
    window.gameCapture.progress('Waiting for game video');
    const video = document.querySelector('video');
    video.srcObject = stream;
    await video.play();
    window.gameCapture.progress('Reading the video frame');
    await new Promise(resolve => setTimeout(resolve, 500));
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth; canvas.height = video.videoHeight;
    if (!canvas.width || !canvas.height) throw new Error('The game window is not visible. Use windowed mode.');
    canvas.getContext('2d').drawImage(video, 0, 0);
    window.gameCapture.complete({ image: canvas.toDataURL('image/png') });
  } catch (error) { window.gameCapture.complete({ error: `Game capture unavailable: ${error.message}. Try windowed mode or import an image.` }); }
  finally { stream?.getTracks().forEach(track => track.stop()); }
};
