.PHONY: parity

# Check ortus/ <-> template/ortus/ parity AND ralph.sh <-> goal.sh structural
# parity. CI and contributors can run `make parity` to detect drift between
# the working copy and the distributable template mirror, or between the two
# orchestrators (FR-022). Both checks always run so users see the full set of
# divergences in one shot; the target's exit code is the OR of the two checks.
# See scripts/check-ortus-parity.sh and scripts/check-structural-parity.sh.
parity:
	@rc=0; \
	bash scripts/check-ortus-parity.sh || rc=$$?; \
	bash scripts/check-structural-parity.sh || rc=$$?; \
	exit $$rc
