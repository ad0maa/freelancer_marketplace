FactoryBot.define do
  factory :booking do
    client
    freelancer
    start_date { Date.tomorrow }
    end_date { 1.week.from_now.to_date }
    total_amount { Faker::Number.decimal(l_digits: 3, r_digits: 2) }
    status { :pending }
  end
end
