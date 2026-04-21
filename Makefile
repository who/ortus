.PHONY: parity

# Check ortus/ <-> template/ortus/ parity.
# CI and contributors can run `make parity` to detect drift between the working
# copy and the distributable template mirror. See scripts/check-ortus-parity.sh.
parity:
	@bash scripts/check-ortus-parity.sh
