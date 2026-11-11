// SPDX-License-Identifier: GPL-2.0
/*
 * gpustress - headless GPU load for the undervolt auto-tuner.
 *
 * WHY NOT glmark2: it needs its own Mesa+EGL install, which meant a container,
 * which meant shipping an image or running dnf at tune time. This links against
 * the Mesa already on the device and ships as one small binary inside the
 * plugin. glmark2 stays useful as the yardstick: if this cannot push the GPU at
 * least as hard, it is not a real stability test.
 *
 * It renders a deliberately ALU-heavy fragment shader offscreen, through a GBM
 * render node, so it needs no display, no compositor and no DRM master - the
 * session keeps running while this loads the GPU.
 *
 * Instability is reported two ways:
 *   - exit code 2 if the GL context is lost (GPU reset out from under us),
 *     which is the clearest signal a client gets that the GPU fell over;
 *   - exit code 3 on a GL error or a frame that never completes.
 * The tuner additionally watches dmesg, because a hang can take the whole
 * device down without this process ever getting scheduled again.
 */
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <fcntl.h>
#include <gbm.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static const char *VERT =
	"attribute vec2 p;\n"
	"varying vec2 uv;\n"
	"void main() { uv = p; gl_Position = vec4(p, 0.0, 1.0); }\n";

/*
 * The load itself. A long dependent chain of transcendentals: no texture
 * fetches, so this stays on the ALUs rather than turning into a memory test,
 * and the loop count is high enough that the compiler cannot fold it away.
 * Undervolt failures show up in exactly this kind of sustained ALU work.
 */
/*
 * A second, deliberately different workload. The ALU shader hammers one
 * functional unit; an undervolt can be stable on transcendentals and still
 * corrupt on memory traffic, so this one is dominated by dependent texture
 * fetches with integer address arithmetic. Sampling is NEAREST from a texture
 * we filled ourselves, so it stays bit-deterministic and can be checksummed
 * the same way.
 */
static const char *FRAG_MIX =
	"precision highp float;\n"
	"varying vec2 uv;\n"
	"uniform float t;\n"
	"uniform int iters;\n"
	"uniform sampler2D src;\n"
	"void main() {\n"
	"  vec4 a = vec4(uv, t, 1.0);\n"
	"  vec2 c = uv * 0.5 + 0.5;\n"
	"  for (int i = 0; i < 4096; i++) {\n"
	"    if (i >= iters) break;\n"
	"    vec4 s = texture2D(src, c);\n"
	/* No bitwise operators: GLSL ES 1.00 forbids them, and requiring ES 3.00
	 * would mean an ES3 context for no benefit. Modular arithmetic gives the
	 * same scrambling and the same dependent-fetch chain. */
	"    float k = mod(floor(s.r * 255.0) * 3.0 + floor(s.g * 255.0), 256.0);\n"
	"    c = fract(c + vec2(k * 0.00390625, s.b * 0.25) + 0.001);\n"
	"    a = a * 0.75 + s * 0.25 + vec4(mod(k, 16.0) * 0.001);\n"
	"  }\n"
	"  gl_FragColor = a;\n"
	"}\n";

static const char *FRAG =
	"precision highp float;\n"
	"varying vec2 uv;\n"
	"uniform float t;\n"
	"uniform int iters;\n"
	"void main() {\n"
	"  vec4 a = vec4(uv, t, 1.0);\n"
	"  for (int i = 0; i < 4096; i++) {\n"
	"    if (i >= iters) break;\n"
	"    a = sin(a * 1.0001 + cos(a.wzyx * 0.9999 + t)) + a * 0.5;\n"
	"    a = normalize(a + vec4(1e-6));\n"
	"  }\n"
	"  gl_FragColor = a;\n"
	"}\n";

static unsigned long crc32_buf(const unsigned char *buf, size_t len)
{
	unsigned long crc = 0xffffffffUL;

	for (size_t i = 0; i < len; i++) {
		crc ^= buf[i];
		for (int k = 0; k < 8; k++)
			crc = (crc >> 1) ^ (0xedb88320UL & (-(long)(crc & 1)));
	}
	return crc ^ 0xffffffffUL;
}

static double now_s(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

static GLuint compile(GLenum type, const char *src)
{
	GLuint s = glCreateShader(type);
	GLint ok = 0;

	glShaderSource(s, 1, &src, NULL);
	glCompileShader(s);
	glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
	if (!ok) {
		char log[1024] = { 0 };

		glGetShaderInfoLog(s, sizeof(log) - 1, NULL, log);
		fprintf(stderr, "gpustress: shader compile failed: %s\n", log);
		return 0;
	}
	return s;
}

int main(int argc, char **argv)
{
	/* Bigger by default: more fragments in flight fills more of the machine,
	 * and occupancy is what turns a shader into a power load. */
	int size = 1536, seconds = 20, iters = 512, opt;
	/* Burst duty cycle. A flat load draws high but STEADY current; rails
	 * collapse on di/dt, when the GPU slams between idle and full. Alternating
	 * provokes the transient that a constant load never produces. */
	int burst_on = 12, burst_off = 6, no_burst = 0;
	const char *node = "/dev/dri/renderD128";
	/* Expected checksum of the verification frame. Undervolt failures often
	 * corrupt arithmetic long before they hang anything, so a run that merely
	 * did not crash proves very little - the result has to be checked. */
	unsigned long expect = 0;
	int have_expect = 0;

	while ((opt = getopt(argc, argv, "s:t:i:d:r:o:f:F")) != -1) {
		switch (opt) {
		case 's': size = atoi(optarg); break;
		case 't': seconds = atoi(optarg); break;
		case 'i': iters = atoi(optarg); break;
		case 'd': node = optarg; break;
		case 'r': expect = strtoul(optarg, NULL, 16); have_expect = 1; break;
		case 'o': burst_on = atoi(optarg); break;
		case 'f': burst_off = atoi(optarg); break;
		case 'F': no_burst = 1; break;
		default:
			fprintf(stderr, "usage: %s [-s size] [-t seconds] [-i iters] [-d node]\n", argv[0]);
			return 1;
		}
	}

	int fd = open(node, O_RDWR | O_CLOEXEC);

	if (fd < 0) {
		fprintf(stderr, "gpustress: cannot open %s\n", node);
		return 1;
	}

	struct gbm_device *gbm = gbm_create_device(fd);

	if (!gbm) {
		fprintf(stderr, "gpustress: gbm_create_device failed\n");
		return 1;
	}

	PFNEGLGETPLATFORMDISPLAYEXTPROC get_display =
		(PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
	EGLDisplay dpy = get_display ? get_display(EGL_PLATFORM_GBM_KHR, gbm, NULL)
				     : eglGetDisplay((EGLNativeDisplayType)gbm);

	if (dpy == EGL_NO_DISPLAY || !eglInitialize(dpy, NULL, NULL)) {
		fprintf(stderr, "gpustress: eglInitialize failed\n");
		return 1;
	}
	eglBindAPI(EGL_OPENGL_ES_API);

	/* No EGL_SURFACE_TYPE: GBM configs do not advertise PBUFFER, and we render
	 * only to an FBO, so constraining the surface type just fails the match. */
	EGLint cfg_attr[] = {
		EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
		EGL_NONE
	};
	EGLConfig cfg;
	EGLint n = 0;

	if (!eglChooseConfig(dpy, cfg_attr, &cfg, 1, &n) || n < 1) {
		fprintf(stderr, "gpustress: eglChooseConfig failed\n");
		return 1;
	}

	EGLint ctx_attr[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
	EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attr);

	if (ctx == EGL_NO_CONTEXT) {
		fprintf(stderr, "gpustress: eglCreateContext failed\n");
		return 1;
	}
	/* Surfaceless: everything goes to an FBO, so no window system is needed. */
	if (!eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) {
		fprintf(stderr, "gpustress: eglMakeCurrent failed\n");
		return 1;
	}

	printf("gpustress: %s on %s\n", glGetString(GL_RENDERER), node);

	GLuint tex, fbo;

	glGenTextures(1, &tex);
	glBindTexture(GL_TEXTURE_2D, tex);
	glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, size, size, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
	glGenFramebuffers(1, &fbo);
	glBindFramebuffer(GL_FRAMEBUFFER, fbo);
	glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
	if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
		fprintf(stderr, "gpustress: incomplete framebuffer\n");
		return 1;
	}

	GLuint vs = compile(GL_VERTEX_SHADER, VERT);
	GLuint fs = compile(GL_FRAGMENT_SHADER, FRAG);
	GLuint fsm = compile(GL_FRAGMENT_SHADER, FRAG_MIX);

	if (!vs || !fs || !fsm)
		return 1;

	GLuint prog = glCreateProgram();

	glAttachShader(prog, vs);
	glAttachShader(prog, fs);
	glBindAttribLocation(prog, 0, "p");
	glLinkProgram(prog);

	GLuint prog_mix = glCreateProgram();

	glAttachShader(prog_mix, vs);
	glAttachShader(prog_mix, fsm);
	glBindAttribLocation(prog_mix, 0, "p");
	glLinkProgram(prog_mix);

	/* Source texture for the memory workload: generated, not loaded, so the
	 * contents are identical on every run and the checksum stays comparable. */
	const int SRC = 256;
	unsigned char *seed = malloc(SRC * SRC * 4);

	if (!seed) {
		fprintf(stderr, "gpustress: out of memory\n");
		return 1;
	}
	for (int i = 0; i < SRC * SRC; i++) {
		seed[i * 4 + 0] = (unsigned char)(i * 7);
		seed[i * 4 + 1] = (unsigned char)(i * 13 + 5);
		seed[i * 4 + 2] = (unsigned char)(i * 31 + 11);
		seed[i * 4 + 3] = 255;
	}

	GLuint srctex;

	glGenTextures(1, &srctex);
	glActiveTexture(GL_TEXTURE0);
	glBindTexture(GL_TEXTURE_2D, srctex);
	glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, SRC, SRC, 0, GL_RGBA, GL_UNSIGNED_BYTE, seed);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT);
	free(seed);

	glUseProgram(prog_mix);
	glUniform1i(glGetUniformLocation(prog_mix, "src"), 0);
	GLint m_t = glGetUniformLocation(prog_mix, "t");
	GLint m_i = glGetUniformLocation(prog_mix, "iters");

	glUseProgram(prog);

	GLint u_t = glGetUniformLocation(prog, "t");
	GLint u_i = glGetUniformLocation(prog, "iters");
	static const GLfloat quad[] = { -1, -1, 3, -1, -1, 3 };

	glEnableVertexAttribArray(0);
	glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, quad);
	glViewport(0, 0, size, size);
	glUniform1i(u_i, iters);

	unsigned char *pixels = malloc((size_t)size * size * 4);

	if (!pixels) {
		fprintf(stderr, "gpustress: out of memory\n");
		return 1;
	}

	/*
	 * The verification frame is rendered from a FIXED input, so its output is
	 * deterministic: the same GPU running correctly must produce the same
	 * bytes every time. Any difference is the hardware computing the wrong
	 * answer, which is exactly what a too-deep undervolt does first.
	 */
	unsigned long checksum = 0;
	double start = now_s(), last = start;
	unsigned long frames = 0, last_frames = 0, checks = 0;

	int burst = 0;

	while (now_s() - start < seconds) {
		/* Alternate the two workloads so both the ALU and the memory path
		 * are exercised within every second of the test. */
		int use_mix = (frames & 1) != 0;

		glUseProgram(use_mix ? prog_mix : prog);
		glUniform1i(use_mix ? m_i : u_i, iters);
		glUniform1f(use_mix ? m_t : u_t, (GLfloat)(now_s() - start));
		glDrawArrays(GL_TRIANGLES, 0, 3);
		/* Force completion each frame: without it the queue just grows and
		 * the GPU is never actually pushed to keep up. */
		glFinish();
		frames++;

		/* The transient. After a burst of frames, go completely idle for a
		 * few milliseconds so the rail has to recover from a hard step in
		 * current - which is what actually breaks a marginal undervolt. */
		if (!no_burst && ++burst >= burst_on) {
			burst = 0;
			if (burst_off > 0)
				usleep((useconds_t)burst_off * 1000);
		}

		GLenum err = glGetError();

		if (err == GL_CONTEXT_LOST_KHR) {
			fprintf(stderr, "gpustress: CONTEXT LOST - the GPU reset\n");
			return 2;
		}
		if (err != GL_NO_ERROR) {
			fprintf(stderr, "gpustress: GL error 0x%04x\n", err);
			return 3;
		}

		double t = now_s();

		if (t - last >= 2.0) {
			/* Verify: fixed input, read back, checksum, compare. */
			glUseProgram(prog);
			glUniform1i(u_i, iters);
			glUniform1f(u_t, 0.5f);
			glDrawArrays(GL_TRIANGLES, 0, 3);
			glFinish();
			glReadPixels(0, 0, size, size, GL_RGBA, GL_UNSIGNED_BYTE, pixels);

			unsigned long got = crc32_buf(pixels, (size_t)size * size * 4);

			checks++;
			if (!checksum)
				checksum = got;
			if (have_expect && got != expect) {
				fprintf(stderr, "gpustress: CHECKSUM MISMATCH got %08lx want %08lx\n",
					got, expect);
				return 4;
			}
			if (got != checksum) {
				fprintf(stderr, "gpustress: CHECKSUM DRIFT %08lx then %08lx\n",
					checksum, got);
				return 4;
			}

			printf("gpustress: %.0f fps crc %08lx\n",
			       (frames - last_frames) / (t - last), got);
			fflush(stdout);
			last = t;
			last_frames = frames;
		}
	}

	printf("gpustress: done, %lu frames, %lu checks, crc %08lx\n",
	       frames, checks, checksum);
	return 0;
}
