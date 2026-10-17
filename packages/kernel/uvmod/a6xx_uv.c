// SPDX-License-Identifier: GPL-2.0
/*
 * a6xx_uv - out-of-tree runtime GX undervolt for Adreno a830 (SM8750).
 *
 * WHY OUT OF TREE: every in-kernel attempt to add this to a6xx_gmu.c failed to
 * boot, including a version whose only content was an unread module parameter,
 * reproducibly, from identical source. The working theory is a latent memory
 * bug in the msm/GMU path whose symptom depends on code layout, so ANY edit to
 * that translation unit is a coin flip. A separate module leaves the kernel
 * binary byte-identical and can be insmod/rmmod'd in seconds.
 *
 * HOW IT WORKS: kprobe a6xx_hfi_start(struct a6xx_gmu *gmu, int state), which
 * is the call that hands the perf table to GMU firmware. On the first hit we
 * snapshot the stock gx_arc_votes[]. On every hit we rewrite the live table.
 *
 * THE UNDERVOLT IS EXPRESSED AS A SHIFT BETWEEN OPPs, not a raw corner index:
 *
 *     gx_arc_votes[i] = stock[i - delta[i]]
 *
 * i.e. "run frequency i at the voltage corner frequency i-delta already uses".
 * Every value written is therefore a corner pair Qualcomm already ships for
 * this GPU, with primary and secondary consistent. No cmd-db access, and no
 * way to synthesise a corner the chip never uses (in particular, no way to
 * reach RPMH_REGULATOR_LEVEL_OFF while the GPU is clocked).
 *
 * Deltas are >= 0 and clamped so i-delta never goes below the lowest real OPP.
 */
#include <linux/module.h>
#include <linux/kprobes.h>
#include <linux/moduleparam.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>

#include "adreno/a6xx_gmu.h"
#include "adreno/a6xx_gpu.h"

#define UV_MAX_FREQS	GMU_MAX_GX_FREQS
/*
 * 4 was arbitrary and turned out to be the binding constraint: a tuning sweep
 * found real limits only at the top four operating points, and everything from
 * 443 to 832 MHz simply ran out of ceiling. The floor rule (src >= lowest real
 * OPP) is what actually keeps this safe, so the cap only needs to be high
 * enough not to be the answer.
 */
#define UV_MAX_SHIFT	8

static int uv_shift[UV_MAX_FREQS];
static int uv_shift_count;
/*
 * A SPINLOCK, not a mutex. uv_pre() runs as a kprobe pre-handler, which the
 * kernel calls with preemption disabled; mutex_lock() may sleep, and sleeping
 * there is undefined behaviour on a CONFIG_PREEMPT kernel. This build has
 * CONFIG_DEBUG_ATOMIC_SLEEP off, so the violation was completely silent - it
 * only showed up as unexplained instability under contention, which is exactly
 * when userspace reads the vote parameters while the GMU is booting.
 *
 * Every critical section here is a short memcpy or a sysfs_emit loop, so a
 * spinlock costs nothing and is safe from both contexts.
 */
static DEFINE_SPINLOCK(uv_lock);

static u32 uv_stock[UV_MAX_FREQS];
static u32 uv_applied[UV_MAX_FREQS];
static unsigned long uv_freqs[UV_MAX_FREQS];
static struct a6xx_gmu *uv_gmu;	/* cached so exit() can restore stock */
static int uv_stock_n;
static bool uv_have_stock;
static unsigned long uv_hits;

/*
 * WHAT THE MODULE THINKS OF THE KERNEL IT LANDED IN.
 *
 * vermagic cannot tell our build from Armada's stock one: same "7.2.0 SMP
 * preempt mod_unload aarch64", same config, so the loader's checks pass and
 * insmod succeeds either way. That is fine right up until struct a6xx_gmu's
 * layout differs between the two, at which point every offset below points at
 * the wrong thing - and the loader has already said yes.
 *
 * So the check is on the DATA, not on the build identity: a real vote table
 * has ascending frequencies and corners that never fall as frequency rises.
 * Garbage read through wrong offsets does not. If it does not validate we go
 * inert - hook still registered, nothing ever written - and say so through the
 * "status" parameter so the UI can report an incompatible kernel instead of
 * claiming the undervolt is live.
 */
enum {
	UV_WAITING = 0,	/* hooked; the GMU has not booted since we loaded */
	UV_OK,		/* snapshot taken, and it looks like a real vote table */
	UV_INCOMPAT,	/* it did not - staying inert */
};
static int uv_state;

static int uv_shift_set(const char *val, const struct kernel_param *kp)
{
	unsigned long flags;
	int tmp[UV_MAX_FREQS] = { };
	char *buf, *cur, *tok;
	int n = 0, v, ret = 0;

	buf = kstrdup_and_replace(val, '\n', '\0', GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	cur = buf;
	while ((tok = strsep(&cur, ",")) != NULL) {
		if (!*tok)
			continue;
		if (n >= UV_MAX_FREQS) { ret = -E2BIG; goto out; }
		if (kstrtoint(strim(tok), 10, &v)) { ret = -EINVAL; goto out; }
		if (v < 0 || v > UV_MAX_SHIFT) { ret = -ERANGE; goto out; }
		tmp[n++] = v;
	}

	spin_lock_irqsave(&uv_lock, flags);
	memcpy(uv_shift, tmp, sizeof(uv_shift));
	uv_shift_count = n;
	spin_unlock_irqrestore(&uv_lock, flags);
out:
	kfree(buf);
	return ret;
}

static int uv_shift_get(char *buffer, const struct kernel_param *kp)
{
	unsigned long flags;
	int i, len = 0;

	spin_lock_irqsave(&uv_lock, flags);
	for (i = 0; i < uv_shift_count; i++)
		len += sysfs_emit_at(buffer, len, i ? ",%d" : "%d", uv_shift[i]);
	spin_unlock_irqrestore(&uv_lock, flags);
	len += sysfs_emit_at(buffer, len, "\n");
	return len;
}

static const struct kernel_param_ops uv_shift_ops = {
	.set = uv_shift_set,
	.get = uv_shift_get,
};
module_param_cb(shift, &uv_shift_ops, NULL, 0644);
MODULE_PARM_DESC(shift,
	"Per-OPP undervolt as an OPP shift, ascending frequency order. 0=stock, 1=use the next-lower OPP's voltage corner, etc. Max 8.");

/* read-only introspection */
static int uv_stock_get(char *buffer, const struct kernel_param *kp)
{
	unsigned long flags;
	int i, len = 0;

	spin_lock_irqsave(&uv_lock, flags);
	for (i = 0; i < uv_stock_n; i++)
		len += sysfs_emit_at(buffer, len, i ? ",0x%08x" : "0x%08x", uv_stock[i]);
	spin_unlock_irqrestore(&uv_lock, flags);
	len += sysfs_emit_at(buffer, len, "\n");
	return len;
}
static const struct kernel_param_ops uv_stock_ops = { .get = uv_stock_get };
module_param_cb(stock_votes, &uv_stock_ops, NULL, 0444);

/* what the hook actually wrote into the live table on its last run */
static int uv_applied_get(char *buffer, const struct kernel_param *kp)
{
	unsigned long flags;
	int i, len = 0;

	spin_lock_irqsave(&uv_lock, flags);
	for (i = 0; i < uv_stock_n; i++)
		len += sysfs_emit_at(buffer, len, i ? ",0x%08x" : "0x%08x", uv_applied[i]);
	spin_unlock_irqrestore(&uv_lock, flags);
	len += sysfs_emit_at(buffer, len, "\n");
	return len;
}
static const struct kernel_param_ops uv_applied_ops = { .get = uv_applied_get };
module_param_cb(applied_votes, &uv_applied_ops, NULL, 0444);

module_param_named(hits, uv_hits, ulong, 0444);
MODULE_PARM_DESC(hits, "number of times the hook has fired");

/*
 * The operating points the shift list is indexed by. Userspace needs these to
 * label its controls, and it has to be this list rather than devfreq's: an
 * entry here is a slot in gmu->gx_arc_votes[], and index 0 is the rail-off
 * entry that has no devfreq counterpart. Reading it from anywhere else risks
 * an off-by-one that would move a vote onto the wrong frequency.
 */
static int uv_freqs_get(char *buffer, const struct kernel_param *kp)
{
	unsigned long flags;
	int i, len = 0;

	spin_lock_irqsave(&uv_lock, flags);
	for (i = 0; i < uv_stock_n; i++)
		len += sysfs_emit_at(buffer, len, i ? ",%lu" : "%lu", uv_freqs[i]);
	spin_unlock_irqrestore(&uv_lock, flags);
	len += sysfs_emit_at(buffer, len, "\n");
	return len;
}
static const struct kernel_param_ops uv_freqs_ops = { .get = uv_freqs_get };
module_param_cb(gpu_freqs, &uv_freqs_ops, NULL, 0444);
MODULE_PARM_DESC(gpu_freqs, "Hz per shift index; empty until the hook first fires");

/*
 * Does this look like a GMU perf table, or like whatever happens to sit at
 * those offsets in a kernel we were not built against?
 *
 * Both properties hold for every real table and are very unlikely to survive
 * being read through the wrong offsets:
 *   - gpu_freqs[] ascends (index 0 may be the rail-off entry, so 0 is allowed)
 *   - a clocked OPP always carries a non-zero corner, and corners never fall
 *     as frequency rises
 * Deliberately encoding-agnostic: it never assumes what a vote's bits mean,
 * only that the table is ordered, which is what makes it survive an upstream
 * change to the vote format.
 */
static bool uv_table_plausible(struct a6xx_gmu *gmu, int n)
{
	int i, real = 0;

	for (i = 0; i < n; i++) {
		if (!gmu->gpu_freqs[i])
			continue;
		if (i && gmu->gpu_freqs[i - 1] &&
		    gmu->gpu_freqs[i] <= gmu->gpu_freqs[i - 1])
			return false;
		if (!gmu->gx_arc_votes[i])
			return false;
		if (i && gmu->gpu_freqs[i - 1] &&
		    gmu->gx_arc_votes[i] < gmu->gx_arc_votes[i - 1])
			return false;
		real++;
	}
	/* one operating point is not a curve; a real table has the full set */
	return real >= 2;
}

static int uv_status_get(char *buffer, const struct kernel_param *kp)
{
	unsigned long flags;
	int st;

	spin_lock_irqsave(&uv_lock, flags);
	st = uv_state;
	spin_unlock_irqrestore(&uv_lock, flags);

	switch (st) {
	case UV_OK:
		return sysfs_emit(buffer, "ok\n");
	case UV_INCOMPAT:
		return sysfs_emit(buffer, "incompatible\n");
	default:
		return sysfs_emit(buffer, "waiting\n");
	}
}
static const struct kernel_param_ops uv_status_ops = { .get = uv_status_get };
module_param_cb(status, &uv_status_ops, NULL, 0444);
MODULE_PARM_DESC(status,
	"waiting = hooked but the GMU has not booted yet; ok = live; incompatible = the GMU table did not validate, module is inert");

static int uv_pre(struct kprobe *p, struct pt_regs *regs)
{
	unsigned long flags;
	struct a6xx_gmu *gmu = (struct a6xx_gmu *)regs->regs[0];
	int i, n;

	if (!gmu)
		return 0;

	n = gmu->nr_gpu_freqs;
	if (n <= 0 || n > UV_MAX_FREQS) {
		/*
		 * A real GMU never has this many operating points, so we are
		 * reading through offsets that do not belong to this kernel.
		 */
		spin_lock_irqsave(&uv_lock, flags);
		uv_state = UV_INCOMPAT;
		spin_unlock_irqrestore(&uv_lock, flags);
		pr_err_once("a6xx_uv: nr_gpu_freqs=%d is not a real table - built for a different kernel, staying inert\n",
			    n);
		return 0;
	}

	uv_hits++;

	/* Once inert, stay inert: never write through offsets we distrust. */
	if (uv_state == UV_INCOMPAT)
		return 0;

	spin_lock_irqsave(&uv_lock, flags);

	if (!uv_have_stock) {
		if (!uv_table_plausible(gmu, n)) {
			uv_state = UV_INCOMPAT;
			spin_unlock_irqrestore(&uv_lock, flags);
			pr_err_once("a6xx_uv: GMU vote table did not validate - built for a different kernel, staying inert\n");
			return 0;
		}
		memcpy(uv_stock, gmu->gx_arc_votes, n * sizeof(u32));
		memcpy(uv_freqs, gmu->gpu_freqs, n * sizeof(uv_freqs[0]));
		uv_stock_n = n;
		uv_have_stock = true;
		uv_state = UV_OK;
	}

	/*
	 * Only cached once the table is trusted: uv_exit() writes through this
	 * pointer to restore stock votes, and doing that on a mismatched layout
	 * is the one thing this guard exists to prevent.
	 */
	uv_gmu = gmu;

	for (i = 0; i < uv_stock_n; i++) {
		int s, src;

		/*
		 * freqs[0] is the "off" frequency and its vote is the rail-off
		 * vote. Never remap it - shifting it would replace "rail off"
		 * with a live corner.
		 */
		if (!gmu->gpu_freqs[i]) {
			gmu->gx_arc_votes[i] = uv_stock[i];
			continue;
		}

		s = (i < uv_shift_count) ? uv_shift[i] : 0;
		src = i - s;

		/*
		 * Never drop below the lowest REAL operating point. src==0 is
		 * the rail-off vote: using it here would switch the GX rail
		 * off with the GPU still clocked.
		 */
		if (src < 1)
			src = 1;
		while (src < i && !gmu->gpu_freqs[src])
			src++;
		if (src > i)
			src = i;
		gmu->gx_arc_votes[i] = uv_stock[src];
	}

	memcpy(uv_applied, gmu->gx_arc_votes, uv_stock_n * sizeof(u32));

	spin_unlock_irqrestore(&uv_lock, flags);
	return 0;
}

static struct kprobe uv_kp = {
	.symbol_name = "a6xx_hfi_start",
	.pre_handler = uv_pre,
};

static int __init uv_init(void)
{
	int ret = register_kprobe(&uv_kp);

	if (ret) {
		pr_err("a6xx_uv: register_kprobe failed: %d\n", ret);
		return ret;
	}
	pr_info("a6xx_uv: hooked %s at %p\n", uv_kp.symbol_name, uv_kp.addr);
	return 0;
}

static void __exit uv_exit(void)
{
	unsigned long flags;
	unregister_kprobe(&uv_kp);

	/*
	 * Put the stock votes back. The driver only rebuilds gx_arc_votes at
	 * probe, so anything we leave behind persists until reboot - and a
	 * reload would then snapshot OUR values as "stock" and shift again,
	 * compounding every cycle.
	 */
	spin_lock_irqsave(&uv_lock, flags);
	if (uv_gmu && uv_have_stock && uv_state == UV_OK) {
		memcpy(uv_gmu->gx_arc_votes, uv_stock, uv_stock_n * sizeof(u32));
		pr_info("a6xx_uv: restored stock votes on unload\n");
	}
	spin_unlock_irqrestore(&uv_lock, flags);

	pr_info("a6xx_uv: unhooked after %lu hits\n", uv_hits);
}

module_init(uv_init);
module_exit(uv_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Runtime GX undervolt for Adreno a830 via kprobe");
