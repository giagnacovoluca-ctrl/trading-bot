#!/bin/bash
cd /home/ubuntu/GIT/video_generator

# FFmpeg script to concatenate 4 images with zoompan for 12 seconds each (total 48 seconds)
# Frame rate 30fps

ffmpeg -y -loop 1 -t 12 -i assets/backgrounds/glowing_biological_clock_1785697214890.jpg \
       -loop 1 -t 12 -i assets/backgrounds/1_dna_helix.jpg \
       -loop 1 -t 12 -i assets/backgrounds/1_glowing_brain.jpg \
       -loop 1 -t 12 -i assets/backgrounds/2_ancient_ancestor.jpg \
       -filter_complex "[0:v]zoompan=z='min(zoom+0.0005,1.5)':d=360:s=1080x1920:fps=30[v0]; \
                        [1:v]zoompan=z='min(zoom+0.0005,1.5)':d=360:s=1080x1920:fps=30[v1]; \
                        [2:v]zoompan=z='min(zoom+0.0005,1.5)':d=360:s=1080x1920:fps=30[v2]; \
                        [3:v]zoompan=z='min(zoom+0.0005,1.5)':d=360:s=1080x1920:fps=30[v3]; \
                        [v0][v1][v2][v3]concat=n=4:v=1:a=0,format=yuv420p[v]" \
       -map "[v]" -c:v libx264 -preset fast -pix_fmt yuv420p assets/backgrounds/multiscena_nano_banana.mp4
