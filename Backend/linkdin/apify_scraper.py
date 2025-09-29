# from apify_client import ApifyClient
# import json

# # Initialize the ApifyClient with your API token
# client = ApifyClient("apify_api_xzoLbzmGGSTuP6gAsBqvCqcIgSXXoK3RtuV4")

# # Define the Actor ID and input parameters
# actor_id = "4LvRT5GN3rhH6QmY5"
# run_input = {
#     "url": "https://www.linkedin.com/company/netcom-learning/",
#     "number": 10
# }

# # Run the Actor and wait for it to finish
# run = client.actor(actor_id).call(run_input=run_input)

# # Retrieve and print the results from the dataset
# dataset_items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
# with open("output.json", "w", encoding="utf-8") as f:
#     json.dump(dataset_items, f, ensure_ascii=False, indent=4)




from apify_client import ApifyClient

# Initialize the ApifyClient with your API token
client = ApifyClient("apify_api_xzoLbzmGGSTuP6gAsBqvCqcIgSXXoK3RtuV4")

# Prepare the Actor input
run_input = {
    "company_name": "google",
    "page_number": 1,
    "limit": 100,
    "sort": "recent",
}

# Run the Actor and wait for it to finish
run = client.actor("mrThmKLmkxJPehxCg").call(run_input=run_input)

# Fetch and print Actor results from the run's dataset (if there are any)
for item in client.dataset(run["defaultDatasetId"]).iterate_items():
    print(item)