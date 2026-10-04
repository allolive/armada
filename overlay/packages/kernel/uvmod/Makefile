obj-m += a6xx_uv.o
# a6xx_gmu.h pulls in adreno_gpu.h, which includes the register headers that
# kbuild GENERATES into drivers/gpu/drm/msm/generated at build time.
ccflags-y += -I$(srctree)/drivers/gpu/drm/msm \
             -I$(srctree)/drivers/gpu/drm/msm/adreno \
             -I$(srctree)/drivers/gpu/drm/msm/generated
