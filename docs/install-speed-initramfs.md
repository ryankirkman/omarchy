# Initramfs caching investigation

Do not add a blanket initramfs cache. First measure the image candidate's actual boot-finalization substep: it already includes an upstream fix that removes unnecessary encryption work from unencrypted installs. No running image, package, installer, or benchmark configuration was changed during this investigation.

## Observed baseline and source pins

The official 4.0.2 baseline recorded `Finalizing Limine boot = 229.8751699924469 s` in a total installer duration of `1300.3847267627716 s`, on the four-CPU TCG fixture. Its installed root was unencrypted Btrfs on `/dev/vda2`; its final build nevertheless included `encrypt`. The resulting `omarchy_linux.efi` was 41,438,208 bytes. This is one baseline observation, not a measured cache benefit.

| Source | Exact revision |
| --- | --- |
| Omarchy ISO PR145 | `dbffaa6c65344d644627a023c28661e08382b8fa` |
| mkinitcpio 41.1 | `6625f6e585be9825bc40cd3c8bde872192e15e36` |
| limine-entry-tool / limine-mkinitcpio-hook 1.37.1 | `26b1879c862a55bae4e8777d48b2f917c68ef347` |
| Baseline kernel package | `linux 7.1.9.arch1-2` |

`limine-update` deploys the EFI loader and then invokes `limine-mkinitcpio`. Its install script obtains the current command line, builds the initramfs and UKI, invokes the normal post hooks, and updates the Limine entry. The 229.9 seconds therefore cannot be attributed entirely to compression or even entirely to initramfs assembly. [Pinned Limine implementation](https://gitlab.com/Zesko/limine-entry-tool/-/tree/26b1879c862a55bae4e8777d48b2f917c68ef347/install/arch-linux/limine-mkinitcpio-hook).

## Existing upstream improvement to measure first

The baseline's `encrypt` hook includes all kernel crypto modules when `CRYPTO_MODULES` is unset, plus cryptsetup and its dependencies. PR145's `_configure_initramfs_encryption_hooks` removes `encrypt` and `sd-encrypt` only after establishing that the target is unencrypted. Encrypted and protected LUKS targets retain their unlock hooks. This could reduce the candidate's boot-build work, but the amount must be measured. Credit belongs to the existing upstream change. [mkinitcpio's exact encrypt hook](https://github.com/archlinux/mkinitcpio/blob/v41.1/install/encrypt), [pinned PR145 phase implementation](https://github.com/omacom/omarchy-iso/blob/dbffaa6c65344d644627a023c28661e08382b8fa/configs/airootfs/usr/share/omarchy-iso/orchestrator/phases_impl.py).

## Why an existing initramfs is not interchangeable

The root-image builder deliberately masks boot-image package hooks. It therefore does not provide a package-built initramfs that can simply be reused. The final build follows hardware setup, hibernation configuration and provisioning because those steps supply its inputs. [Pinned root-image builder](https://github.com/omacom/omarchy-iso/blob/dbffaa6c65344d644627a023c28661e08382b8fa/builder/build-root-image.sh).

Relevant inputs include the selected kernel and DKMS modules; hardware autodetection and CPU microcode; modprobe configuration; keyboard layout and console font; resume hooks; encryption mode; and optional per-install provisioning keyfiles. Omarchy places `keyboard` before `autodetect`, retaining broad keyboard support for the unlock prompt. Moving that hook would change the keyboard guarantee. A generic image may contain a larger set of drivers, but it would still need correct target configuration and newly installed hardware modules. [Autodetection](https://github.com/archlinux/mkinitcpio/blob/v41.1/install/autodetect), [keyboard hook](https://github.com/archlinux/mkinitcpio/blob/v41.1/install/keyboard), [mkinitcpio configuration contract](https://man.archlinux.org/man/mkinitcpio.conf.5.en).

UKI handling also remains target-specific. Limine deliberately omits an embedded command line when Secure Boot and Snapper require that behavior, and relies on mkinitcpio post hooks for signing. Replacing its final call with a bare UKI assembly command would not preserve that complete contract. Reusing an already signed UKI would also retain the wrong per-target boot inputs.

## Next bounded diagnostic

Wait for the candidate's normal boot-branch timing. If it remains material, use a separate disposable copy to time the EFI deployment, each `run_build_hook`, `install_modules`, `build_image`, `build_uki`, signing/post hooks, and menu update. Add timing only at those boundaries, avoiding a full shell trace that could dominate the work. Save a normal build tree with mkinitcpio's supported `--save` option for comparison; this is a debugging artifact, not a supported cache-reuse interface. [mkinitcpio's options](https://man.archlinux.org/man/mkinitcpio.8.en), [pinned build pipeline](https://github.com/archlinux/mkinitcpio/blob/v41.1/mkinitcpio).

Only then choose an experiment. If compression matters, a separate bounded zstd-worker/level variant preserves every hook and signing step, with decompressed payload equality and a real boot required. If hook construction dominates, any cache prototype must first demonstrate exact input matching, fresh hardware detection, misses on changed kernel/modules/configuration/locale, and exclusion of reusable secrets; it must still rebuild the target UKI and run the existing signing and Limine hooks. That prototype is not part of the current running candidate, and no cache speedup is claimed here.
