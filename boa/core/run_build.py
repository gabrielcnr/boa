import os
import glob
import itertools

from mamba.mamba_api import PrefixData

from boa.core.render import render
from boa.core.utils import get_config
from boa.core.recipe_output import Output, CondaBuildSpec
from boa.core.solver import refresh_solvers
from boa.core.build import build, download_source
from boa.core.metadata import MetaData
from boa.core.test import run_test

from conda_build.utils import rm_rf
import conda_build.jinja_context
from conda.common import toposort
from conda.models.match_spec import MatchSpec
from conda.gateways.disk.create import mkdir_p
from conda_build.variants import get_default_variant

from conda_build.index import update_index

from rich.console import Console
from rich.table import Table

console = Console()


def find_all_recipes(target, config):
    if os.path.isdir(target):
        cwd = target
    else:
        cwd = os.getcwd()
    yamls = glob.glob(os.path.join(cwd, "recipe.yaml"))
    yamls += glob.glob(os.path.join(cwd, "**", "recipe.yaml"))

    recipes = {}
    for fn in yamls:
        yml = render(fn, config=config)
        pkg_name = yml["package"]["name"]
        recipes[pkg_name] = yml
        recipes[pkg_name]["recipe_file"] = fn
        # find all outputs from recipe
        output_names = set([yml["package"]["name"]])
        for output in yml.get("outputs", []):
            output_names.add(output["package"]["name"])

        if "static" in [f["name"] for f in yml.get("features", [])]:
            output_names.add(yml["package"]["name"] + "-static")

        recipes[pkg_name]["output_names"] = output_names

    sort_recipes = {}

    def get_all_requirements(x):
        req = x.get("requirements", {}).get("host", [])
        req += x.get("requirements", {}).get("run", [])
        for feat in x.get("features", []):
            req += feat.get("requirements", {}).get("host", [])
            req += feat.get("requirements", {}).get("run", [])
        for o in x.get("outputs", []):
            req += get_all_requirements(o)
        return req

    def recursive_add(target):
        all_requirements = {
            x.split(" ")[0] for x in get_all_requirements(recipes[target])
        }
        all_requirements = all_requirements.intersection(recipes.keys())
        sort_recipes[target] = all_requirements
        for req in all_requirements:
            if req not in sort_recipes:
                recursive_add(req)

    if not target or target not in recipes.keys():
        for k in recipes.keys():
            recursive_add(k)
    else:
        recursive_add(target)

    sorted_recipes = toposort.toposort(sort_recipes)
    print(sorted_recipes)

    return [recipes[x] for x in sorted_recipes]


def get_dependency_variants(requirements, conda_build_config, config, features=()):
    host = requirements.get("host") or []
    build = requirements.get("build") or []
    # run = requirements.get("run") or []

    variants = {}
    default_variant = get_default_variant(config)

    # When compiling for OS X, we should fetch the clang compilers ...
    # I think this needs a more thorough rework
    # if config.variant["target_platform"] == "osx-64":
    # default_variant.update(
    #     {
    #         "c_compiler": "clang",
    #         "cxx_compiler": "clangxx",
    #         "fortran_compiler": "gfortran",
    #     },
    # )

    variants["target_platform"] = conda_build_config.get(
        "target_platform", [default_variant["target_platform"]]
    )

    if conda_build_config["target_platform"] == [None]:
        variants["target_platform"] = [default_variant["target_platform"]]

    config.variant["target_platform"] = variants["target_platform"][0]

    def get_variants(env):
        specs = {}

        for s in env:
            spec = CondaBuildSpec(s)
            specs[spec.name] = spec

        for n, cb_spec in specs.items():
            if cb_spec.is_compiler:
                # This is a compiler package
                _, lang = cb_spec.raw.split()
                compiler = conda_build.jinja_context.compiler(lang, config)
                cb_spec.final = compiler
                config_key = f"{lang}_compiler"
                config_version_key = f"{lang}_compiler_version"

                if conda_build_config.get(config_key):
                    variants[config_key] = conda_build_config[config_key]
                if conda_build_config.get(config_version_key):
                    variants[config_version_key] = conda_build_config[
                        config_version_key
                    ]

            # Note: as a historical artifact we __have to__ use underscore-replaced
            # names here!
            variant_key = n.replace("-", "_")
            vlist = None
            if variant_key in conda_build_config:
                vlist = conda_build_config[variant_key]
            elif variant_key in default_variant:
                vlist = [default_variant[variant_key]]
            if vlist:
                # we need to check if v matches the spec
                if cb_spec.is_simple:
                    variants[variant_key] = vlist
                elif cb_spec.is_pin:
                    # ignore variants?
                    pass
                else:
                    # check intersection of MatchSpec and variants
                    ms = MatchSpec(cb_spec.raw)
                    filtered = []
                    for var in vlist:
                        vsplit = var.split()
                        if len(vsplit) == 1:
                            p = {
                                "name": n,
                                "version": vsplit[0],
                                "build_number": 0,
                                "build": "",
                            }
                        elif len(vsplit) == 2:
                            p = {
                                "name": n,
                                "version": var.split()[0],
                                "build": var.split()[1],
                                "build_number": 0,
                            }
                        else:
                            raise RuntimeError("Check your conda_build_config")

                        if ms.match(p):
                            filtered.append(var)
                        else:
                            console.print(
                                f"Configured variant ignored because of the recipe requirement:\n  {cb_spec.raw} : {var}\n"
                            )

                    if len(filtered):
                        variants[variant_key] = filtered

        return variants

    v = get_variants(host + build)
    return v


def to_build_tree(ydoc, variants, config, cbc, selected_features):
    for k in variants:
        table = Table(show_header=True, header_style="bold")
        table.title = f"Output: [bold white]{k}[/bold white]"
        table.add_column("Package")
        table.add_column("Variant versions")
        for pkg, var in variants[k].items():
            table.add_row(pkg, "\n".join(var))
        console.print(table)

    # first we need to perform a topological sort taking into account all the outputs
    if ydoc.get("outputs"):
        outputs = [
            Output(
                o,
                config,
                parent=ydoc,
                conda_build_config=cbc,
                selected_features=selected_features,
            )
            for o in ydoc["outputs"]
        ]
        outputs = {o.name: o for o in outputs}
    else:
        outputs = [
            Output(
                ydoc,
                config,
                conda_build_config=cbc,
                selected_features=selected_features,
            )
        ]
        outputs = {o.name: o for o in outputs}

    if len(outputs) > 1:
        sort_dict = {
            k: [x.name for x in o.all_requirements()] for k, o in outputs.items()
        }
        tsorted = toposort.toposort(sort_dict)
        tsorted = [o for o in tsorted if o in sort_dict.keys()]
    else:
        tsorted = [o for o in outputs.keys()]

    final_outputs = []

    for name in tsorted:
        output = outputs[name]
        if variants.get(output.name):
            v = variants[output.name]
            combos = []

            differentiating_keys = []
            for k in v:
                if len(v[k]) > 1:
                    differentiating_keys.append(k)
                combos.append([(k, x) for x in v[k]])

            all_combinations = tuple(itertools.product(*combos))
            all_combinations = [dict(x) for x in all_combinations]
            for c in all_combinations:
                x = output.apply_variant(c, differentiating_keys)
                final_outputs.append(x)
        else:
            x = output.apply_variant({})
            final_outputs.append(x)

    temp = final_outputs
    final_outputs = []
    has_intermediate = False
    for o in temp:
        if o.sections["build"].get("intermediate"):
            if has_intermediate:
                raise RuntimeError(
                    "Already found an intermediate build. There can be only one!"
                )
            final_outputs.insert(0, o)
            has_intermediate = True
        else:
            final_outputs.append(o)

    # Note: maybe this should happen _before_ apply variant?!
    if has_intermediate:
        # inherit dependencies
        def merge_requirements(a, b):
            b_names = [x.name for x in b]
            for r in a:
                if r.name in b_names:
                    continue
                else:
                    b.append(r)

        intermediate = final_outputs[0]
        for o in final_outputs[1:]:
            merge_requirements(
                intermediate.requirements["host"], o.requirements["host"]
            )
            merge_requirements(
                intermediate.requirements["build"], o.requirements["build"]
            )
            merged_variant = {}
            merged_variant.update(intermediate.config.variant)
            merged_variant.update(o.config.variant)
            o.config.variant = merged_variant

    return final_outputs


def build_recipe(args, recipe_path, cbc, config):

    if args.features:
        assert args.features.startswith("[") and args.features.endswith("]")
        features = [f.strip() for f in args.features[1:-1].split(",")]
    else:
        features = []

    selected_features = {}
    for f in features:
        if f.startswith("~"):
            selected_features[f[1:]] = False
        else:
            selected_features[f] = True

    ydoc = render(recipe_path, config=config)
    # We need to assemble the variants for each output
    variants = {}
    # if we have a outputs section, use that order the outputs
    if ydoc.get("outputs"):
        for o in ydoc["outputs"]:
            # inherit from global package
            pkg_meta = {}
            pkg_meta.update(ydoc["package"])
            pkg_meta.update(o["package"])
            o["package"] = pkg_meta

            build_meta = {}
            build_meta.update(ydoc.get("build"))
            build_meta.update(o.get("build") or {})
            o["build"] = build_meta

            o["selected_features"] = selected_features

            variants[o["package"]["name"]] = get_dependency_variants(
                o.get("requirements", {}), cbc, config, features
            )
    else:
        # we only have one output
        variants[ydoc["package"]["name"]] = get_dependency_variants(
            ydoc.get("requirements", {}), cbc, config, features
        )

    # this takes in all variants and outputs, builds a dependency tree and returns
    # the final metadata
    sorted_outputs = to_build_tree(ydoc, variants, config, cbc, selected_features)

    # then we need to solve and build from the bottom up
    # we can't first solve all packages without finalizing everything
    #
    # FLOW:
    # =====
    # - solve the package
    #   - solv build, add weak run exports to
    # - add run exports from deps!

    if args.command == "render":
        for o in sorted_outputs:
            console.print(o)

    # TODO this should be done cleaner
    top_name = ydoc["package"]["name"]
    o0 = sorted_outputs[0]
    o0.is_first = True
    o0.config.compute_build_id(top_name)

    console.print("\n[yellow]Initializing mamba solver[/yellow]\n")

    # Do not download source if we might skip
    if not args.skip_existing:
        console.print("\n[yellow]Downloading source[/yellow]\n")
        download_source(MetaData(recipe_path, o0), args.interactive)
        cached_source = o0.sections["source"]
    else:
        cached_source = {}
    for o in sorted_outputs:
        console.print(
            f"\n[yellow]Preparing environment for [bold]{o.name}[/bold][/yellow]\n"
        )
        refresh_solvers()
        o.finalize_solve(sorted_outputs)

        o.config._build_id = o0.config.build_id

        meta = MetaData(recipe_path, o)
        o.set_final_build_id(meta)

        if args.skip_existing:
            final_name = meta.dist()
            if os.path.exists(
                os.path.join(
                    o.config.output_folder,
                    o.variant["target_platform"],
                    final_name + ".tar.bz2",
                )
            ):
                console.print(f"\n[green]Skipping existing {final_name}\n")
                continue

        if "build" in o.transactions:
            if os.path.isdir(o.config.build_prefix):
                rm_rf(o.config.build_prefix)
            mkdir_p(o.config.build_prefix)
            try:
                o.transactions["build"]["transaction"].execute(
                    PrefixData(o.config.build_prefix),
                    o.transactions["build"]["pkg_cache"],
                )
            except Exception:
                # This currently enables windows-multi-build...
                print("Could not instantiate build environment")

        if "host" in o.transactions:
            mkdir_p(o.config.host_prefix)
            o.transactions["host"]["transaction"].execute(
                PrefixData(o.config.host_prefix), o.transactions["host"]["pkg_cache"],
            )

        if cached_source != o.sections["source"]:
            download_source(meta, args.interactive)
            cached_source = o.sections["source"]

        console.print(f"\n[yellow]Starting build for [bold]{o.name}[/bold][/yellow]\n")

        final_outputs = build(meta, None, allow_interactive=args.interactive)

        stats = {}
        if final_outputs is not None:
            for final_out in final_outputs:
                run_test(
                    final_out, o.config, stats, move_broken=False, provision_only=False,
                )
        # print(stats)

    for o in sorted_outputs:
        print("\n\n")
        console.print(o)


def run_build(args):
    folder = args.recipe_dir
    variant = {}
    if args.target_platform:
        variant["target_platform"] = args.target_platform

    cbc, config = get_config(folder, variant)
    cbc["target_platform"] = [args.target_platform]

    if not os.path.exists(config.output_folder):
        mkdir_p(config.output_folder)

    console.print(f"Updating build index: {(config.output_folder)}\n")
    update_index(config.output_folder, verbose=config.debug, threads=1)

    all_recipes = find_all_recipes(args.target, config)  # [noqa]

    console.print("\n[yellow]Assembling all recipes and variants[/yellow]\n")

    for recipe in all_recipes:
        build_recipe(args, recipe["recipe_file"], cbc, config)
