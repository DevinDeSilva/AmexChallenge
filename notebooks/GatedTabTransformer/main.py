import torch
import torch.nn as nn
import torch.optim as optim
import pandas as pd

import ray
from ray import tune

import inquirer
from gated_tab_transformer import GatedTabTransformer
from train_with_validation import train, validate
from data_utils import get_unique_categorical_counts, get_categ_cont_target_values, train_val_test_split
from metadata import datasets

device = torch.device("cuda")
LOG_INTERVAL = 10
MAX_EPOCHS = 200
RANDOMIZE_SAMPLES = False
DATASET = "BLASTCHAR"

data = datasets[DATASET]
dataset = pd.read_csv(data["PATH"], header=0)

if RANDOMIZE_SAMPLES:
    # Randomize order
    dataset = dataset.sample(frac=1)

n_categories = get_unique_categorical_counts(dataset, data["CONT_COUNT"])

train_dataframe, val_dataframe, test_dataframe = train_val_test_split(dataset)

train_cont, train_categ, train_target = get_categ_cont_target_values(train_dataframe, data["POSITIVE_CLASS"],
                                                                     data["CONT_COUNT"])
val_cont, val_categ, val_target = get_categ_cont_target_values(val_dataframe, data["POSITIVE_CLASS"],
                                                               data["CONT_COUNT"])
test_cont, test_categ, test_target = get_categ_cont_target_values(test_dataframe, data["POSITIVE_CLASS"],
                                                                  data["CONT_COUNT"])


def train_experiment(config):
    model = GatedTabTransformer(
        categories=n_categories,  # tuple containing the number of unique values within each category
        num_continuous=train_cont.shape[1],  # number of continuous values
        transformer_dim=config["transformer_dim"],  # dimension, paper set at 32
        dim_out=1,  # binary prediction, but could be anything
        transformer_depth=config["transformer_depth"],  # depth, paper recommended 6
        transformer_heads=config["transformer_heads"],  # heads, paper recommends 8
        attn_dropout=config["dropout"],  # post-attention dropout
        ff_dropout=config["dropout"],  # feed forward dropout
        mlp_act=nn.LeakyReLU(config["relu_slope"]),
        # activation for final mlp, defaults to relu, but could be anything else (selu, etc.)
        mlp_depth=config["mlp_depth"],  # mlp hidden layers depth
        mlp_dimension=config["mlp_dimension"],  # dimension of mlp layers
        gmlp_enabled=config["gmlp_enabled"]  # gmlp or standard mlp
    )

    model = model.train().to(device=device)
    criterion = nn.BCEWithLogitsLoss()
    optimizer = optim.AdamW(params=model.parameters(), lr=config["initial_lr"], amsgrad=True)
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=config["scheduler_step"],
                                          gamma=config["scheduler_gamma"])

    trained_model_dict = train(
        model,
        criterion,
        optimizer,
        scheduler,
        train_cont,
        train_categ,
        train_target,
        val_cont,
        val_categ,
        val_target,
        device=device,
        batch_size=config["batch_size"],
        max_epochs=MAX_EPOCHS,
        patience=config["patience"],
        save_best_model_dict=False,
        save_metrics=False,
        log_interval=LOG_INTERVAL
    )

    model.load_state_dict(trained_model_dict)
    score = validate(model, test_cont, test_categ, test_target, device=device, save_metrics=True)

    tune.report(auc=score)
    print("Final score", score)


# HPO training example summarizing all the aspects of the paper
if __name__ == "__main__":
    print("Currently loaded dataset -", DATASET)

    questions = [
        inquirer.List("experiment",
                      message="Please select an experiment",
                      choices=[
                          ("GatedTabTransformer POC with gMLP", "gated_poc"),
                          ("TabTransformer POC without gMLP", "regular_poc"),
                          # ("Ray HPO: Demo", "hpo_demo"),
                          ("Ray HPO: All paper parameters", "hpo_all"),
                      ],
                      ),
    ]

    answers = inquirer.prompt(questions)
    print("Running experiment...")

    if answers["experiment"] == "hpo_all":
        ray.init(address="auto")
        analysis = tune.run(
            train_experiment,
            num_samples=15,
            log_to_file=True,
            resources_per_trial={"gpu": 1},
            config={
                "batch_size": tune.choice([128, 256]),
                "patience": tune.choice([2, 5, 10, 15]),
                "initial_lr": tune.grid_search([5e-2, 1e-2, 5e-3, 1e-3, 5e-4]),
                "scheduler_gamma": tune.grid_search([0.5, 0.2, 0.1]),
                "scheduler_step": tune.grid_search([5, 10, 15]),
                "relu_slope": tune.grid_search([0.01, 0.02, 0.05, 0.1, 0.2]),
                "transformer_heads": tune.grid_search([4, 8, 12, 16]),
                "transformer_depth": tune.grid_search([4, 6, 8]),
                "transformer_dim": tune.grid_search([8, 16, 32, 64, 128]),
                "gmlp_enabled": tune.grid_search([False, True]),
                "mlp_depth": tune.grid_search([2, 4, 6, 8]),
                "mlp_dimension": tune.grid_search([8, 16, 32, 64, 128, 256]),
                "dropout": tune.grid_search([0.0, 0.1, 0.2, 0.5])
            }
        )

        print("Best config: ", analysis.get_best_config(metric="auc", mode="max"))

    if answers["experiment"] == "gated_poc":
        train_experiment({
            "batch_size": 256,
            "patience": 5,
            "initial_lr": 1e-3,
            "scheduler_gamma": 0.1,
            "scheduler_step": 8,
            "relu_slope": 0,
            "transformer_heads": 8,
            "transformer_depth": 6,
            "transformer_dim": 8,
            "gmlp_enabled": True,
            "mlp_depth": 6,
            "mlp_dimension": 64,
            "dropout": 0.2,
        })

    if answers["experiment"] == "regular_poc":
        train_experiment({
            "batch_size": 256,
            "patience": 5,
            "initial_lr": 1e-3,
            "scheduler_gamma": 0.1,
            "scheduler_step": 8,
            "relu_slope": 0,
            "transformer_heads": 8,
            "transformer_depth": 6,
            "transformer_dim": 8,
            "gmlp_enabled": False,
            "mlp_depth": 6,
            "mlp_dimension": 128,
            "dropout": 0.2,
        })
